"""Remembered projects and non-invasive project inspection."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from fastapi import HTTPException

from backend import providers
from backend.workspace_paths import is_link_or_junction

SETTINGS_FILE = providers.DATA_DIR / "settings.json"
RECENT_LIMIT = 12

MANIFESTS = (
    "pyproject.toml", "requirements.txt", "Pipfile", "setup.py", "package.json",
    "pnpm-lock.yaml", "yarn.lock", "package-lock.json", "Cargo.toml",
    "go.mod", "pom.xml", "build.gradle", "Gemfile", "composer.json",
    "*.sln", "*.csproj",
)


def _read() -> dict:
    if not SETTINGS_FILE.exists():
        return {"active_project": None, "recent_projects": []}
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        data.setdefault("active_project", None)
        data.setdefault("recent_projects", [])
        return data
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(500, "Could not read local project settings.") from exc


def _write(data: dict) -> None:
    providers.DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp = SETTINGS_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temp.replace(SETTINGS_FILE)


def resolve_project(value: str) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(422, "The selected project folder does not exist or cannot be opened.") from exc
    if not path.is_dir():
        raise HTTPException(422, "Choose an existing project folder.")
    return path


def open_project(value: str) -> dict:
    path = resolve_project(value)
    data = _read()
    recent = [item for item in data["recent_projects"] if item.get("path") != str(path)]
    recent.insert(0, {"path": str(path), "name": path.name or str(path)})
    data["active_project"] = str(path)
    data["recent_projects"] = recent[:RECENT_LIMIT]
    _write(data)
    return project_status()


def close_project() -> None:
    data = _read()
    data["active_project"] = None
    _write(data)


def remove_recent(value: str) -> None:
    data = _read()
    try:
        path = str(Path(value).expanduser().resolve(strict=False))
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(422, "Invalid recent project path.") from exc
    data["recent_projects"] = [item for item in data["recent_projects"] if item.get("path") != path]
    _write(data)


def active_path() -> Path:
    value = _read().get("active_project")
    if not value:
        raise HTTPException(409, "Open a project folder before starting a task.")
    path = resolve_project(value)
    return path


def project_status() -> dict:
    data = _read()
    active = data.get("active_project")
    return {"active_project": active,
            "active_name": Path(active).name if active else None,
            "recent_projects": data["recent_projects"]}


def git_branch(root: Path) -> str:
    """Read a local branch name without invoking Git or following repository links."""
    git_dir = root / ".git"
    if not git_dir.is_dir() or is_link_or_junction(git_dir):
        return ""

    def read_git_file(path: Path) -> str:
        try:
            path.resolve(strict=True).relative_to(git_dir.resolve(strict=True))
            current = git_dir
            for part in path.relative_to(git_dir).parts:
                current = current / part
                if is_link_or_junction(current):
                    return ""
            return path.read_text(encoding="utf-8").strip()
        except (OSError, RuntimeError, ValueError, UnicodeError):
            return ""

    head = read_git_file(git_dir / "HEAD")
    if not head.startswith("ref: refs/heads/"):
        return "(detached HEAD)" if head else ""
    reference = head[5:]
    branch = reference.removeprefix("refs/heads/")
    if not branch or any(part in {"", ".", ".."} for part in Path(reference).parts):
        return ""
    value = read_git_file(git_dir / Path(reference))
    if value:
        return branch
    packed = read_git_file(git_dir / "packed-refs")
    if any(line.split(" ", 1)[-1] == reference for line in packed.splitlines()
           if line and not line.startswith(("#", "^"))):
        return branch
    return ""


def inspect_project(value: str) -> dict:
    root = resolve_project(value)

    def safe_path(path: Path) -> bool:
        try:
            path.resolve(strict=True).relative_to(root)
        except (OSError, RuntimeError, ValueError):
            return False
        current = root
        for part in path.relative_to(root).parts:
            current = current / part
            if is_link_or_junction(current):
                return False
        return True

    files = []
    for pattern in MANIFESTS:
        files.extend(root.glob(pattern))
    manifests = sorted({path.name for path in files if path.is_file() and safe_path(path)})

    extensions: dict[str, int] = {}
    scanned = 0
    ignored = {".git", ".luna", ".venv", "venv", "node_modules", "__pycache__",
               "target", "dist", "build"}
    for directory, names, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        names[:] = [name for name in names if name.lower() not in ignored
                    and not is_link_or_junction(current / name)]
        for filename in filenames:
            if not safe_path(current / filename):
                continue
            scanned += 1
            if scanned > 3000:
                break
            suffix = Path(filename).suffix.lower()
            if suffix:
                extensions[suffix] = extensions.get(suffix, 0) + 1
        if scanned > 3000:
            break
    language_map = {".py": "Python", ".js": "JavaScript", ".jsx": "React",
                    ".ts": "TypeScript", ".tsx": "React / TypeScript", ".cs": "C#",
                    ".go": "Go", ".rs": "Rust", ".java": "Java", ".rb": "Ruby",
                    ".php": "PHP", ".html": "HTML", ".css": "CSS"}
    languages = sorted({language_map[ext] for ext in extensions if ext in language_map})

    entries = []
    for name in ("src", "app", "backend", "frontend", "main.py", "app.py", "index.html",
                 "README.md", "AGENTS.md", "package.json", "pyproject.toml"):
        if (root / name).exists():
            entries.append(name)
    package_script_names = []
    dependencies = set()
    package_json = root / "package.json"
    if package_json.is_file() and safe_path(package_json):
        try:
            package = json.loads(package_json.read_text(encoding="utf-8"))
            package_script_names = sorted(package.get("scripts", {}).keys())
            dependencies.update(name.lower() for name in package.get("dependencies", {}))
            dependencies.update(name.lower() for name in package.get("devDependencies", {}))
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
            package_script_names = []
    frameworks = sorted(name for name in ("react", "next", "vue", "svelte", "angular",
                                           "vite", "express", "fastapi", "django", "flask")
                        if name in dependencies)
    for manifest in ("requirements.txt", "pyproject.toml", "Pipfile"):
        candidate = root / manifest
        if candidate.is_file() and safe_path(candidate) and candidate.stat().st_size < 100_000:
            try:
                text = candidate.read_text(encoding="utf-8").lower()
            except (OSError, UnicodeError):
                continue
            for framework in ("fastapi", "django", "flask"):
                if re.search(r"\b" + framework + r"(?:[\s<>=~!]|$)", text):
                    frameworks.append(framework)
    frameworks = sorted(set(frameworks))

    branch = git_branch(root)
    git_status = f"Branch: {branch} (working tree status not checked)" if branch else None

    return {"path": str(root), "name": root.name, "languages": languages,
            "manifests": manifests[:50], "entry_points": entries,
            "test_commands": [cmd for cmd in ("python -m pytest" if "pyproject.toml" in manifests or "requirements.txt" in manifests else None,
                                               "npm test" if "package.json" in manifests and "test" in package_script_names else None)
                              if cmd],
            "frameworks": frameworks, "package_script_names": package_script_names,
            "git_status": git_status,
            "has_agent_instructions": (root / "AGENTS.md").is_file()}
