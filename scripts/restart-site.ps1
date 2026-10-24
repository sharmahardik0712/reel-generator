# Restart the live site's container now, so the newest deploy goes live immediately.
#   powershell -ExecutionPolicy Bypass -File scripts\restart-site.ps1
# Anything in progress on the old container (a render, an upload, unexpired reels) is lost.
# Normally not needed: deploys switch over by themselves once nothing is in use.
param([string]$Site = "https://reel.sharmahardik0712.workers.dev")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$secret = (Get-Content (Join-Path $root ".reel-secret") -Raw).Trim()

$answer = Invoke-RestMethod -Method Post -Uri "$Site/__reel/restart" `
    -Headers @{ Authorization = "Bearer $secret" } -UserAgent "Mozilla/5.0 (reel-restart)"
Write-Host $answer
Write-Host "Open $Site (or https://makemyreel.in); the first load takes up to a minute."
