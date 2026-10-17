"""Local skills registry. Skill files are data and are never executed."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from fastapi import HTTPException

from backend import providers

SKILLS_DIR = providers.DATA_DIR / "skills"
SKILL_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_FILES = 100
MAX_TOTAL = 1_000_000
MAX_INSTRUCTIONS = 20_000
SECRET_TEXT = re.compile(
    r"(?i)\bsk-[A-Za-z0-9_-]{16,}\b|\bBearer\s+[A-Za-z0-9._~-]{16,}|"
    r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password)\s*[:=]\s*['\"]?[^\s,'\";]+"
)


def _root() -> Path:
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    return SKILLS_DIR.resolve()


def _path(skill_id: str) -> Path:
    if not SKILL_ID.fullmatch(skill_id):
        raise HTTPException(422, "Skill ID must contain only letters, numbers, hyphens, or underscores.")
    root = _root()
    candidate = root / skill_id
    if candidate.is_symlink() or getattr(candidate, "is_junction", lambda: False)():
        raise HTTPException(422, "Linked skill directories are not allowed.")
    target = candidate.resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise HTTPException(422, "Skill path is outside the registry.") from exc
    return target


def _metadata(folder: Path) -> dict:
    file = folder / "skill.json"
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HTTPException(422, "Skill metadata is missing or invalid JSON.") from exc
    if not isinstance(data, dict):
        raise HTTPException(422, "Skill metadata must be an object.")
    name = data.get("name")
    desc = data.get("description")
    version = data.get("version")
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise HTTPException(422, "Skill name must contain 1 to 80 characters.")
    if not isinstance(desc, str) or not desc.strip() or len(desc) > 500:
        raise HTTPException(422, "Skill description must contain 1 to 500 characters.")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise HTTPException(422, "Skill version must use major.minor.patch format.")
    tools = data.get("tools", [])
    if not isinstance(tools, list) or any(not isinstance(item, str) or len(item) > 80 for item in tools):
        raise HTTPException(422, "Skill tools must be a list of short names.")
    if len(tools) != len(set(tools)):
        raise HTTPException(422, "Skill tool names must be unique.")
    return {"name": name.strip(), "description": desc.strip(), "version": version,
            "tools": tools, "enabled": bool(data.get("enabled", False))}


def _instructions(folder: Path) -> str:
    file = folder / "SKILL.md"
    if file.is_symlink() or not file.is_file():
        raise HTTPException(422, "Skill must include a regular SKILL.md file.")
    text = file.read_text(encoding="utf-8")
    if not text.strip() or len(text) > MAX_INSTRUCTIONS:
        raise HTTPException(422, "Skill instructions must contain 1 to 20,000 characters.")
    if SECRET_TEXT.search(text):
        raise HTTPException(422, "Skill instructions cannot contain credential-like text.")
    return text


def _validate_tree(folder: Path) -> tuple[list[Path], int]:
    entries = list(folder.rglob("*"))
    if len(entries) > MAX_FILES:
        raise HTTPException(422, "Skill directory contains too many entries.")
    total = 0
    protected = {".env", ".npmrc", ".pypirc", "id_rsa", "id_ed25519"}
    for entry in entries:
        if entry.is_symlink() or getattr(entry, "is_junction", lambda: False)():
            raise HTTPException(422, "Skill directories cannot contain symlinks or junctions.")
        if {part.lower() for part in entry.relative_to(folder).parts} & {".git", ".luna"}:
            raise HTTPException(422, "Skill directories cannot contain Git metadata or Luna data.")
        if entry.is_file():
            if (entry.name.lower() in protected or entry.name.lower().startswith(".env.")
                    or entry.suffix.lower() in {".pem", ".p12", ".pfx", ".key"}):
                raise HTTPException(422, "Credential files cannot be installed as skill support files.")
            total += entry.stat().st_size
            if total > MAX_TOTAL:
                raise HTTPException(422, "Skill directory exceeds the 1 MB size limit.")
            if entry.stat().st_size <= MAX_TOTAL:
                try:
                    text = entry.read_text(encoding="utf-8")
                except (UnicodeError, OSError):
                    continue
                if SECRET_TEXT.search(text):
                    raise HTTPException(422, "Skill files cannot contain credential-like text.")
    return entries, total


def list_skills() -> list[dict]:
    result = []
    for folder in sorted(_root().iterdir()):
        if not folder.is_dir() or folder.is_symlink():
            continue
        item = _metadata(folder)
        item["id"] = folder.name
        result.append(item)
    return result


def inspect_skill(skill_id: str) -> dict:
    folder = _path(skill_id)
    if not folder.is_dir():
        raise HTTPException(404, "Skill not found.")
    item = _metadata(folder)
    _validate_tree(folder)
    item["id"] = skill_id
    item["instructions"] = _instructions(folder)
    item["files"] = sorted(str(path.relative_to(folder)) for path in folder.rglob("*")
                           if path.is_file() and not path.is_symlink())
    return item


def install_skill(source: str) -> dict:
    candidate = Path(source).expanduser().absolute()
    parent = candidate
    while parent != parent.parent:
        if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
            raise HTTPException(422, "Linked skill directories are not allowed.")
        parent = parent.parent
    folder = candidate.resolve(strict=True)
    if not folder.is_dir():
        raise HTTPException(422, "Choose a local skill directory.")
    meta = _metadata(folder)
    _instructions(folder)
    _validate_tree(folder)
    base = re.sub(r"[^a-z0-9_-]+", "-", meta["name"].lower()).strip("-_")[:64]
    skill_id = base or "skill"
    if not SKILL_ID.fullmatch(skill_id):
        raise HTTPException(422, "Skill name cannot be converted to a safe ID.")
    target = _path(skill_id)
    if target.exists():
        raise HTTPException(409, "A skill with this ID is already installed.")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(folder, target)
    except OSError as exc:
        shutil.rmtree(target, ignore_errors=True)
        raise HTTPException(400, "Could not copy the local skill directory.") from exc
    data = _metadata(target)
    data["enabled"] = False
    (target / "skill.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    return {"id": skill_id, **data}


def set_enabled(skill_id: str, enabled: bool) -> dict:
    folder = _path(skill_id)
    if not folder.is_dir():
        raise HTTPException(404, "Skill not found.")
    item = _metadata(folder)
    _instructions(folder)
    item["enabled"] = enabled
    (folder / "skill.json").write_text(json.dumps(item, indent=2), encoding="utf-8")
    return {"id": skill_id, **item}


def remove_skill(skill_id: str) -> None:
    folder = _path(skill_id)
    if not folder.is_dir():
        raise HTTPException(404, "Skill not found.")
    if folder.is_symlink():
        raise HTTPException(409, "Refusing to remove a linked skill directory.")
    shutil.rmtree(folder)


def enabled_instructions() -> list[tuple[str, str]]:
    result = []
    total = 0
    for skill in list_skills():
        if skill["enabled"]:
            details = inspect_skill(skill["id"])
            total += len(details["instructions"])
            if total > 40_000:
                raise HTTPException(413, "Enabled skill instructions exceed the 40 KB task limit.")
            result.append((skill["id"], details["instructions"]))
    return result
