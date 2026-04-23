# GO_DRYRUN_V23.ps1 — start v23 in DRY-RUN mode (no real orders).
# Run from the PropBot folder on your local machine or VPS.
#
# Usage:   .\GO_DRYRUN_V23.ps1
#
# The MT5 terminal with SHF_Bridge.mq5 attached to a chart must be running
# and connected to the broker BEFORE you start this script.

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

Write-Host "================================================================" -ForegroundColor Cyan
Write-Host "  v23 DRY-RUN   (4-pair ORB + Merton-GZ + news rails)" -ForegroundColor Cyan
Write-Host "  strategy has ZERO real-order side effects in this mode." -ForegroundColor Cyan
Write-Host "================================================================" -ForegroundColor Cyan

# Sanity: require Python on PATH
$py = (Get-Command python -ErrorAction SilentlyContinue).Path
if (-not $py) { Write-Error "python not on PATH"; exit 1 }

# Run the launcher
& python "$PSScriptRoot\Scripts\run_v23_live.py" `
    --symbols "DE40,US30,XAUUSD,US500" `
    --risk 0.00110 `
    --cap-mult 3.0 `
    --account-kill 0.08 `
    --daily-breaker 0.02 `
    --magic 23000 `
    --news-csv "data/news/tier1_2026.csv" `
    --heartbeat 60
