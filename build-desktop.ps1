$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Run .\run-desktop.ps1 once to create .venv and install desktop dependencies." }
& $python -m pip install -r requirements-packaging.txt -r requirements-browser.txt
if ($LASTEXITCODE -ne 0) { throw "Could not install packaging dependencies." }
& $python -m PyInstaller --noconfirm --clean --distpath (Join-Path $PSScriptRoot "dist") --workpath (Join-Path $PSScriptRoot "build\pyinstaller") desktop\luna-backend.spec
if ($LASTEXITCODE -ne 0) { throw "Could not build LunaBackend.exe." }
& $python -m PyInstaller --noconfirm --clean --distpath (Join-Path $PSScriptRoot "dist") --workpath (Join-Path $PSScriptRoot "build\pyinstaller") desktop\luna-desk.spec
if ($LASTEXITCODE -ne 0) { throw "Could not build Luna Desk." }
$package = Join-Path $PSScriptRoot "dist\LunaDesk"
$backend = Join-Path $PSScriptRoot "dist\LunaBackend"
Get-ChildItem -LiteralPath $backend -Force | Copy-Item -Destination $package -Recurse -Force
Write-Host "Built $package\LunaDesk.exe and its loopback backend. Chromium must be installed separately with: .\.venv\Scripts\python.exe -m playwright install chromium"
