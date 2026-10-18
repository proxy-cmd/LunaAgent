"""Path checks shared by workspace tools and the native file explorer."""

from __future__ import annotations

import os
from pathlib import Path


def is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    junction_check = getattr(path, "is_junction", None)
    if junction_check is not None and junction_check():
        return True
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        get_attributes = ctypes.windll.kernel32.GetFileAttributesW
        get_attributes.argtypes = [wintypes.LPCWSTR]
        get_attributes.restype = wintypes.DWORD
        attributes = get_attributes(str(path))
        return attributes != 0xFFFFFFFF and bool(attributes & 0x400)
    except (AttributeError, OSError, TypeError, ValueError):
        # Fail closed if Windows cannot determine whether this is a reparse point.
        return True
