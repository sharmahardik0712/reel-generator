# One-time setup: creates the shared secret and a task that starts Reel Maker at login.
#   powershell -ExecutionPolicy Bypass -File scripts\install-startup.ps1
# Remove later with:  Unregister-ScheduledTask -TaskName "Reel Maker" -Confirm:$false
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

$secretFile = Join-Path $root ".reel-secret"
if (-not (Test-Path $secretFile)) {
    $bytes = New-Object byte[] 32
    # Create().GetBytes works on Windows PowerShell 5.1 and PowerShell 7 alike.
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    ($bytes | ForEach-Object { $_.ToString("x2") }) -join "" | Set-Content $secretFile -NoNewline
    Write-Host "Created $secretFile (keep it private; it's in .gitignore)."
}

$script = Join-Path $PSScriptRoot "start-reel.ps1"
$powershell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$action = New-ScheduledTaskAction -Execute $powershell -WorkingDirectory $root `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "Reel Maker" -Action $action -Trigger $trigger -Settings $settings `
    -Description "Starts the Reel Maker web app for the public Cloudflare site" -Force | Out-Null
Start-ScheduledTask -TaskName "Reel Maker"
Write-Host "Reel Maker will now start automatically when you log in (running now)."
Write-Host "Next: give the Worker the same secret:"
Write-Host "  cd cloudflare; Get-Content ..\.reel-secret | npx wrangler secret put REEL_PROXY_SECRET"
