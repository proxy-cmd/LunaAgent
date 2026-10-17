$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { throw "Python 3.11 or newer is required." }
$version = & $python.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ([version]$version -lt [version]"3.11") { throw "Python 3.11 or newer is required. Found $version." }
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    & $python.Source -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create .venv." }
}
$appPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $appPython -c "import importlib.util,sys; sys.exit(0 if all(importlib.util.find_spec(x) for x in ('fastapi','httpx','uvicorn','PySide6')) else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing Luna Desk desktop dependencies into .venv..."
    & $appPython -m pip install -r requirements-desktop.txt
    if ($LASTEXITCODE -ne 0) { throw "Desktop dependency installation failed." }
}
& $appPython -m desktop
