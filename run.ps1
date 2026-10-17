param(
    [switch]$UseCurrentPython,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    throw "Python 3.11 or newer is required. Install Python, then run this script again."
}

$versionText = & $python.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
$version = [version]$versionText
if ($version -lt [version]"3.11") {
    throw "Python 3.11 or newer is required. Found Python $versionText."
}

if ($UseCurrentPython) {
    $appPython = $python.Source
    & $appPython -c "import importlib.util, sys; sys.exit(0 if all(importlib.util.find_spec(x) for x in ('fastapi', 'httpx', 'uvicorn')) else 1)"
    if ($LASTEXITCODE -ne 0) {
        throw "FastAPI, HTTPX, or Uvicorn is missing. Install requirements.txt or run without -UseCurrentPython."
    }
} else {
    if (-not (Test-Path ".venv\Scripts\python.exe")) {
        & $python.Source -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw "Could not create the local virtual environment." }
    }

    $appPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
    & $appPython -c "import importlib.util, sys; sys.exit(0 if all(importlib.util.find_spec(x) for x in ('fastapi', 'httpx', 'uvicorn')) else 1)"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Installing Luna Desk dependencies into .venv..."
        & $appPython -m pip install -r requirements.txt
        if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
    }
}

Write-Host "Luna Desk is starting at http://127.0.0.1:8765"
if (-not $NoBrowser) {
    Start-Process "http://127.0.0.1:8765"
}
& $appPython -m uvicorn backend.main:app --host 127.0.0.1 --port 8765
