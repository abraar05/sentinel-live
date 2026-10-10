<#
.SYNOPSIS
  Sets up and runs the Sentinel Live gateway on this machine.

.DESCRIPTION
  Run this on a computer that can reach your NVR/DVR over the LAN. It does
  not need to be reachable from the internet - see deploy/REMOTE-ACCESS.md
  for how to reach it from your phone.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy/windows/install-and-run.ps1
#>

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $Root

function Say($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

# ---------------------------------------------------------------- python
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Say "Python 3.11+ is required. Install from https://python.org"
    Say "then re-run this script."
    exit 1
}
$ver = (& python -c "import sys;print('%d.%d' % sys.version_info[:2])")
Say "found python $ver"

Say "installing dependencies"
& python -m pip install --quiet --upgrade pip
& python -m pip install --quiet -r requirements.txt

# ---------------------------------------------------------------- config
$env:SENTINEL_DATA = Join-Path $Root "data"
if (-not $env:SENTINEL_ADMIN_PASSWORD) {
    $env:SENTINEL_ADMIN_PASSWORD = Read-Host "Create an admin account - password"
    $env:SENTINEL_BIND = "127.0.0.1"   # safe default: local only
    Say "bound to 127.0.0.1 (this machine only)."
    Say "REMOTE-ACCESS.md explains how to reach it from your phone."
} else {
    Say "using SENTINEL_ADMIN_PASSWORD from the environment"
}

New-Item -ItemType Directory -Force -Path (Join-Path $Root "data") | Out-Null

Say "starting gateway on http://127.0.0.1:8080"
Say "open that URL, sign in, then Import your DeviceManager.xml"
Write-Host ""

& python -m uvicorn backend.app:app --host $env:SENTINEL_BIND --port 8080