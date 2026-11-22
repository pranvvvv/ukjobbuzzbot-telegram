# Start JobPilot automatically when you log in to Windows, and restart it if it crashes.
# Usage (from the project folder):  powershell -ExecutionPolicy Bypass -File .\install-autostart.ps1
# Remove with:                      powershell -ExecutionPolicy Bypass -File .\uninstall-autostart.ps1
$ErrorActionPreference = "Stop"
$TaskName = "JobPilot"
$ProjectDir = $PSScriptRoot

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { $uv = Join-Path $env:USERPROFILE ".local\bin\uv.exe" }
if (-not (Test-Path $uv)) { throw "uv not found. Run setup.ps1 first." }

# Runs hidden (no console window); logs go to %LOCALAPPDATA%\JobPilot\logs\jobpilot.log
$command = "Set-Location -LiteralPath '$ProjectDir'; & '$uv' run python -m jobpilot run"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command `"$command`"" `
    -WorkingDirectory $ProjectDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "JobPilot Telegram job-search agent" -Force | Out-Null

Write-Host "Installed: JobPilot will start at every login (hidden) and restart if it crashes." -ForegroundColor Green
$answer = Read-Host "Start it now? (Y/n)"
if ($answer -notin @("n", "N")) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Started. Send /start to your bot. Logs: $env:LOCALAPPDATA\JobPilot\logs\jobpilot.log"
}
