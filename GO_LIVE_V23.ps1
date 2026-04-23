# GO_LIVE_V23.ps1 — start v23 in LIVE mode (REAL ORDERS).
# DANGER: this will place real orders on the connected broker account.
# Do not run unless you have already done a full DRY-RUN cycle.
#
# Usage:   .\GO_LIVE_V23.ps1

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

Write-Host "================================================================" -ForegroundColor Red
Write-Host "  🔴  v23 LIVE  (REAL ORDERS WILL BE PLACED)" -ForegroundColor Red
Write-Host "  4-pair ORB + Merton-GZ + news rails + 5ers-safe rails" -ForegroundColor Red
Write-Host "================================================================" -ForegroundColor Red

# Double-confirm (typed, not keyboard-shortcut)
Write-Host "`nType  GO LIVE  (all caps, exact) to confirm:" -ForegroundColor Yellow
$ack = Read-Host
if ($ack -cne "GO LIVE") {
    Write-Host "Aborted — anything other than 'GO LIVE' stops us." -ForegroundColor Green
    exit 0
}

# Python check
$py = (Get-Command python -ErrorAction SilentlyContinue).Path
if (-not $py) { Write-Error "python not on PATH"; exit 1 }

& python "$PSScriptRoot\Scripts\run_v23_live.py" `
    --live `
    --symbols "DE40,US30,XAUUSD,US500" `
    --risk 0.00110 `
    --cap-mult 3.0 `
    --account-kill 0.08 `
    --daily-breaker 0.02 `
    --magic 23000 `
    --news-csv "data/news/tier1_2026.csv" `
    --heartbeat 60
