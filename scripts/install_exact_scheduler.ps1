$ErrorActionPreference = "Stop"

$scriptPath = (Resolve-Path (Join-Path $PSScriptRoot "trigger_exact_post.ps1")).Path
$taskName = "ZenithMotorsExactReels"

# 4 exact daily triggers every 6 hours (matching 15 */6 * * * UTC: 05:45, 11:45, 17:45, 23:45 IST)
$times = @("05:45", "11:45", "17:45", "23:45")
$triggers = foreach ($t in $times) {
    New-ScheduledTaskTrigger -Daily -At $t
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$scriptPath`" -Mode reel"

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $triggers `
    -Settings $settings `
    -Description "Fires Zenith Motors GitHub Actions Reel workflow 4 times daily every 6 hours (05:45, 11:45, 17:45, 23:45 IST) with zero GitHub cron delay." `
    -Force | Out-Null

Write-Output "Registered Windows Scheduled Task '$taskName' with $($times.Count) exact daily triggers: $($times -join ', ') IST"
