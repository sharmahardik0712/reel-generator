# Starts Reel Maker for the public site (used by the start-on-login task).
# The shared secret lets the app trust the visitor IP the Cloudflare Worker sends.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$secretFile = Join-Path $root ".reel-secret"
if (Test-Path $secretFile) {
    $env:REEL_PROXY_SECRET = (Get-Content $secretFile -Raw).Trim()
}

$log = Join-Path $root "reel-data\server.log"
New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null
& (Join-Path $root ".venv\Scripts\reel.exe") ui --no-browser *>> $log
