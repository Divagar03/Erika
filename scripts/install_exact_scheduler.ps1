$ErrorActionPreference = "Stop"

$scriptPath = (Resolve-Path (Join-Path $PSScriptRoot "trigger_exact_post.ps1")).Path
$taskName = "ZenithMotorsExactReels"

# 12 exact daily :45 IST triggers (matching 15 */2 * * * UTC: 01:45, 03:45, ..., 23:45 IST)
$times = @("01:45", "03:45", "05:45", "07:45", "09:45", "11:45", "13:45", "15:45", "17:45", "19:45", "21:45", "23:45")
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
    -Description "Fires Zenith Motors GitHub Actions Reel workflow at exact :45 IST every 2 hours (12 reels/day) with zero GitHub cron delay." `
    -Force | Out-Null

Write-Output "Registered Windows Scheduled Task '$taskName' with $($times.Count) exact daily triggers: $($times -join ', ') IST"
