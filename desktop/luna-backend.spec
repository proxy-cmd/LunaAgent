from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

root = Path.cwd()
frontend = [(str(path), str(Path("frontend") / path.relative_to(root / "frontend").parent))
            for path in (root / "frontend").rglob("*") if path.is_file()]
hidden = []
for package in ("httpx", "uvicorn", "fastapi", "pydantic", "playwright", "pywinauto"):
    try:
        modules = collect_submodules(package)
        hidden.extend(name for name in modules if not name.startswith("pywinauto.tests"))
    except ImportError:
        pass

a = Analysis([str(root / "desktop" / "backend_entry.py")], pathex=[str(root)],
             binaries=[], datas=frontend, hiddenimports=hidden,
             hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=["tkinter"],
             noarchive=False, optimize=1)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="LunaBackend",
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False, argv_emulation=False,
          target_arch=None, codesign_identity=None, entitlements_file=None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="LunaBackend")
