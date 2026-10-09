$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$RuntimeRoot = Join-Path $ProjectRoot ".runtime"
$TestTemp = Join-Path $RuntimeRoot "temp\pytest"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Missing virtual environment: $Python"
}

New-Item -ItemType Directory -Force $TestTemp | Out-Null
$env:APP_RUNTIME_ROOT = $RuntimeRoot
$env:TEMP = Join-Path $RuntimeRoot "temp"
$env:TMP = $env:TEMP
$env:PYTHONPYCACHEPREFIX = Join-Path $RuntimeRoot "temp\pycache"
Set-Location -LiteralPath $ProjectRoot
& $Python -m pytest --basetemp $TestTemp -q

