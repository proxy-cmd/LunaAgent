"""Optional UI Automation access to windows already open on this desktop."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from fastapi import HTTPException

from backend import providers

enabled = False
selected_handle: int | None = None
SCREEN_DIR = providers.DATA_DIR / "desktop" / "screenshots"
SECRET = re.compile(
    r"(?i)\bsk-[A-Za-z0-9_-]{16,}\b|\bBearer\s+[A-Za-z0-9._~-]{16,}|"
    r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password)\s*[:=]\s*['\"]?[^\s,'\";]+"
)


def _desktop():
    if not enabled:
        raise HTTPException(409, "Enable desktop automation in Settings first.")
    if __import__("os").name != "nt":
        raise HTTPException(501, "Windows UI Automation is available only on Windows.")
    try:
        from pywinauto import Desktop
    except ImportError as exc:
        raise HTTPException(503, "pywinauto is not installed. Install requirements-desktop.txt.") from exc
    return Desktop(backend="uia")


def status() -> dict:
    try:
        import pywinauto  # noqa: F401
        installed = True
    except ImportError:
        installed = False
    return {"available": __import__("os").name == "nt" and installed,
            "enabled": enabled, "elevated": False}


def set_enabled(value: bool) -> dict:
    global enabled, selected_handle
    if value and not status()["available"]:
        raise HTTPException(503, "Install pywinauto on Windows before enabling desktop automation.")
    enabled = value
    if not value:
        selected_handle = None
    return status()


def select(handle: str) -> dict:
    global selected_handle
    window = _lookup(handle)
    selected_handle = window.handle
    return {"handle": hex(selected_handle), "title": _clean(window.window_text())[:160]}


def _lookup(value: str):
    try:
        handle = int(value, 0)
        window = _desktop().window(handle=handle)
        if not window.exists(timeout=1):
            raise ValueError
        return window
    except (ValueError, TypeError, OSError) as exc:
        raise HTTPException(404, "The selected desktop window is no longer available.") from exc


def _handle(value: str):
    window = _lookup(value)
    if selected_handle is None or window.handle != selected_handle:
        raise HTTPException(403, "Select this window in Settings before using desktop controls.")
    return window


def _clean(value: str) -> str:
    return SECRET.sub("[redacted]", value)


def windows() -> list[dict]:
    desktop = _desktop()
    result = []
    for window in desktop.windows(top_level_only=True):
        try:
            title = _clean(window.window_text().strip())
            if title:
                result.append({"handle": hex(window.handle), "title": title[:160],
                               "class_name": window.class_name()[:100]})
        except Exception:
            continue
    return result[:100]


def _controls(window) -> list:
    try:
        controls = window.descendants()
    except Exception as exc:
        raise HTTPException(502, "The selected window controls could not be inspected.") from exc
    return controls[:300]


def inspect(handle: str) -> dict:
    window = _handle(handle)
    controls = []
    for index, item in enumerate(_controls(window)):
        try:
            control_type = item.element_info.control_type
            visible_name = item.window_text().strip() if control_type in {
                "Button", "MenuItem", "TabItem", "RadioButton", "CheckBox"} else ""
            controls.append({"index": index, "type": item.element_info.control_type,
                             "name": _clean(visible_name)[:160],
                             "automation_id": str(item.element_info.automation_id or "")[:100],
                             "enabled": bool(item.is_enabled())})
        except Exception:
            continue
    return {"handle": hex(window.handle), "title": _clean(window.window_text())[:160],
            "controls": controls}


def _control(handle: str, index: int):
    if not 0 <= index < 300:
        raise HTTPException(422, "Control index is out of range.")
    controls = _controls(_handle(handle))
    if index >= len(controls):
        raise HTTPException(404, "The selected control is no longer available.")
    return controls[index]


def click(handle: str, index: int) -> dict:
    target = _control(handle, index)
    try:
        target.click_input()
    except Exception as exc:
        raise HTTPException(502, "The selected desktop control could not be clicked.") from exc
    return {"ok": True}


def action_label(handle: str, index: int, action: str, text: str | None = None) -> str:
    window = _handle(handle)
    target = _control(handle, index)
    control_type = target.element_info.control_type
    name = target.window_text().strip() if control_type in {
        "Button", "MenuItem", "TabItem", "RadioButton", "CheckBox"} else "unnamed control"
    label = f"{action} in {window.window_text().strip()[:100]}: {control_type} {name[:100]}"
    if text is not None:
        label += f" with text {_clean(text)[:120]}"
    return label


def type_text(handle: str, index: int, text: str) -> dict:
    if len(text) > 2_000 or SECRET.search(text):
        raise HTTPException(422, "Credential-like or oversized text cannot be typed.")
    target = _control(handle, index)
    try:
        if target.element_info.control_type == "Edit":
            target.set_edit_text(text)
        else:
            target.type_keys(text, with_spaces=True, set_foreground=True)
    except Exception as exc:
        raise HTTPException(502, "The selected desktop control could not accept text.") from exc
    return {"ok": True}


def screenshot(handle: str) -> dict:
    window = _handle(handle)
    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    name = uuid.uuid4().hex + ".png"
    try:
        window.capture_as_image().save(SCREEN_DIR / name)
    except Exception as exc:
        raise HTTPException(502, "Could not capture the selected window.") from exc
    images = sorted(SCREEN_DIR.glob("*.png"), key=lambda item: item.stat().st_mtime, reverse=True)
    for old in images[10:]:
        old.unlink(missing_ok=True)
    return {"name": name}


def screenshot_path(name: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}\.png", name):
        raise HTTPException(422, "Invalid screenshot name.")
    path = (SCREEN_DIR / name).resolve()
    if path.parent != SCREEN_DIR.resolve() or not path.is_file():
        raise HTTPException(404, "Screenshot not found.")
    return path


def tool_specs() -> list[dict]:
    if not enabled or selected_handle is None:
        return []
    integer = {"type": "integer", "minimum": 0, "maximum": 299}
    return [
        {"type": "function", "function": {"name": "desktop_inspect",
         "description": "Inspect accessible controls in a selected window.",
         "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
        {"type": "function", "function": {"name": "desktop_click",
         "description": "Click a selected window control. Requires user approval.",
         "parameters": {"type": "object", "properties": {"index": integer},
                        "required": ["index"], "additionalProperties": False}}},
        {"type": "function", "function": {"name": "desktop_type",
         "description": "Type non-secret text into a selected window control. Requires user approval.",
         "parameters": {"type": "object", "properties": {"index": integer,
            "text": {"type": "string"}}, "required": ["index", "text"], "additionalProperties": False}}},
        {"type": "function", "function": {"name": "desktop_screenshot",
         "description": "Capture a local screenshot of a selected window.",
         "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    ]
