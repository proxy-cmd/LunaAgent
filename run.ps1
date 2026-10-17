$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    throw "Python 3.11 or newer is required. Install Python, then run this script again."
}

$versionText = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
$version = [version]$versionText
if ($version -lt [version]"3.11") {
    throw "Python 3.11 or newer is required. Found Python $versionText."
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    & python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create the local virtual environment." }
}

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $venvPython -c "import fastapi, httpx, uvicorn" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing Luna Desk dependencies into .venv..."
    & $venvPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
}

Write-Host "Luna Desk is starting at http://127.0.0.1:8765"
& $venvPython -m uvicorn backend.main:app --host 127.0.0.1 --port 8765
