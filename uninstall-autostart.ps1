# Remove the JobPilot autostart task (and stop it if it's running).
$ErrorActionPreference = "Stop"
$TaskName = "JobPilot"
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed the JobPilot autostart task." -ForegroundColor Green
} else {
    Write-Host "No JobPilot autostart task was installed."
}
