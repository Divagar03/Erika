param(
    [string]$Mode = "reel",
    [string]$RepoUrl = "https://github.com/Divagar03/Erika.git",
    [string]$Branch = "main"
)

$ErrorActionPreference = "Stop"

$workDir = Join-Path $env:LOCALAPPDATA "ErikaTrigger"
$logFile = Join-Path $env:LOCALAPPDATA "ErikaTrigger\trigger.log"
New-Item -ItemType Directory -Force -Path $workDir | Out-Null

function Write-Log([string]$msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz') $msg"
    Add-Content -Path $logFile -Value $line -Encoding UTF8
    Write-Output $line
}

try {
    if (-not (Test-Path (Join-Path $workDir ".git"))) {
        Write-Log "Initializing lightweight sparse checkout in $workDir..."
        git clone --depth 1 --filter=blob:none --no-checkout $RepoUrl $workDir | Out-Null
        Push-Location $workDir
        try {
            git sparse-checkout init --no-cone | Out-Null
            git sparse-checkout set /trigger.txt | Out-Null
            git checkout $Branch | Out-Null
            git config user.name "zenith-exact-cron"
            git config user.email "zenith-exact-cron@users.noreply.github.com"
        } finally {
            Pop-Location
        }
    }

    Push-Location $workDir
    try {
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            git fetch origin $Branch --depth 1 | Out-Null
            git reset --hard "origin/$Branch" | Out-Null

            $ts = (Get-Date).ToUniversalTime().ToString("yyyyMMdd_HHmmss")
            $content = "$Mode $ts`n"
            [System.IO.File]::WriteAllText((Join-Path $workDir "trigger.txt"), $content, [System.Text.UTF8Encoding]::new($false))

            git add trigger.txt | Out-Null
            git commit -m "chore: exact trigger ($Mode $ts)" | Out-Null
            git push origin $Branch
            if ($LASTEXITCODE -eq 0) {
                Write-Log "SUCCESS: Pushed exact trigger '$Mode $ts' (attempt $attempt)"
                exit 0
            }
            Write-Log "WARN: git push returned $LASTEXITCODE on attempt $attempt, retrying..."
            Start-Sleep -Seconds 2
        }
        throw "Failed to push exact trigger after 3 attempts"
    } finally {
        Pop-Location
    }
} catch {
    Write-Log "ERROR: $_"
    exit 1
}
