$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$RuntimeRoot = Join-Path $ProjectRoot ".runtime"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Missing virtual environment: $Python"
}

New-Item -ItemType Directory -Force (Join-Path $RuntimeRoot "temp") | Out-Null
$env:APP_RUNTIME_ROOT = $RuntimeRoot
$env:APP_ENABLE_FAULT_INJECTION = "1"
$env:TEMP = Join-Path $RuntimeRoot "temp"
$env:TMP = $env:TEMP
Set-Location -LiteralPath $ProjectRoot
& $Python -m app.worker

