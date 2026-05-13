# DealerClub Scraper VM setup — run as Administrator on vm-worker-2.
# What this does:
#   1. Creates C:\dealerclub
#   2. Downloads the scraper + session cookies + secrets from EW
#   3. Builds a Python venv with Playwright + requests
#   4. Installs headless Chromium (if not already there)
#   5. Runs one smoke-test scrape (verifies API auth + EW POST works)
#   6. Registers a Windows scheduled task that runs the daemon as SYSTEM,
#      starts on boot, restarts on failure forever
#
# After this finishes successfully, the Contabo daemon will be stopped
# (clean cutover) — but tell Claude to flip that switch.

$ErrorActionPreference = 'Stop'
$BASE = 'https://experience-wholesale.net/static/dealerclub-bundle'
$ROOT = 'C:\dealerclub'

Write-Host '=== Step 1: prepare folders ===' -ForegroundColor Cyan
New-Item -ItemType Directory -Path "$ROOT\state" -Force | Out-Null
Set-Location $ROOT

Write-Host '=== Step 2: download scraper bundle ===' -ForegroundColor Cyan
Invoke-WebRequest "$BASE/dealerclub_scraper.py" -OutFile dealerclub_scraper.py
Invoke-WebRequest "$BASE/session.json"          -OutFile state\session.json
Invoke-WebRequest "$BASE/secrets.env"           -OutFile secrets.env

Write-Host '=== Step 3: create Python venv + install deps ===' -ForegroundColor Cyan
python -m venv venv
.\venv\Scripts\pip install --quiet --disable-pip-version-check --upgrade pip
.\venv\Scripts\pip install --quiet --disable-pip-version-check playwright requests

Write-Host '=== Step 4: install headless Chromium ===' -ForegroundColor Cyan
.\venv\Scripts\playwright install chromium

Write-Host '=== Step 5: smoke test (one scrape) ===' -ForegroundColor Cyan
.\venv\Scripts\python dealerclub_scraper.py --once
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Smoke test FAILED — fix before scheduling.' -ForegroundColor Red
    exit 1
}

Write-Host '=== Step 6: register Windows scheduled task ===' -ForegroundColor Cyan
$taskName = 'DealerClubScraper'
# Tear down any prior version first so re-runs are idempotent
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

$action = New-ScheduledTaskAction `
    -Execute "$ROOT\venv\Scripts\python.exe" `
    -Argument "$ROOT\dealerclub_scraper.py --daemon" `
    -WorkingDirectory $ROOT

$trigger = New-ScheduledTaskTrigger -AtStartup
# Plus immediate start on register, so we don't wait for reboot
$triggerNow = New-ScheduledTaskTrigger -Once -At (Get-Date).AddSeconds(15)

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Days 365) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

$principal = New-ScheduledTaskPrincipal `
    -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask -TaskName $taskName `
    -Action $action `
    -Trigger $trigger, $triggerNow `
    -Settings $settings `
    -Principal $principal `
    -Description 'Polls DealerClub live auctions, POSTs each lot to EW.' `
    -Force | Out-Null

Start-ScheduledTask -TaskName $taskName

Start-Sleep 3
$info = Get-ScheduledTaskInfo -TaskName $taskName
Write-Host ''
Write-Host "OK — scheduled task '$taskName' installed." -ForegroundColor Green
Write-Host ("Last run: {0}  Last result: 0x{1:X}" -f $info.LastRunTime, $info.LastTaskResult)
Write-Host ''
Write-Host 'Useful commands:'
Write-Host '  View status:     Get-ScheduledTaskInfo -TaskName DealerClubScraper'
Write-Host '  Watch live:      Get-Process python | Where { $_.Path -like "C:\dealerclub*" }'
Write-Host '  Tail log:        (the daemon writes to stdout/journal — checking Task Scheduler History tab is easiest)'
Write-Host '  Stop:            Stop-ScheduledTask -TaskName DealerClubScraper'
