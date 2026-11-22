# ZenithMotors Long-Form Pipeline Windows Task Scheduler Installer
# Schedules an automatic run every 48 hours

$ErrorActionPreference = "Stop"

$taskName = "ZenithMotorsLongForm48h"
$pythonPath = (Get-Command python).Source
$repoPath = "D:\Erika"
$scriptPath = Join-Path $repoPath "main.py"

Write-Host "Configuring Windows Scheduled Task: $taskName"
Write-Host "Repository: $repoPath"
Write-Host "Python:     $pythonPath"

# Trigger every 2 days at 18:00 (6:00 PM IST)
$trigger = New-ScheduledTaskTrigger -Daily -At "18:00" -DaysInterval 2
$action = New-ScheduledTaskAction -Execute $pythonPath -Argument "$scriptPath youtube_longform" -WorkingDirectory $repoPath
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
Register-ScheduledTask -TaskName $taskName -Trigger $trigger -Action $action -Settings $settings -Description "ZenithMotors 48-Hour Long-Form YouTube Pipeline" | Out-Null

Write-Host "✓ Successfully registered Scheduled Task '$taskName' (Runs every 48 hours)."
