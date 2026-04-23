# GO_DRYRUN_V23.ps1 - start v23 in DRY-RUN mode (no real orders).
# Uses the v24d OPTIMAL sizer config locked-in via V23LiveConfig defaults:
#   base_risk=0.110 pct, cap_mult=5.0 (0.550 pct per-trade cap),
#   gamma=3.0, dd_cap=4 pct, DailyHalt=4 pct, DDBreaker=4 pct.
#
# The MT5 terminal with SHF_Bridge.mq5 attached to a chart must be running
# and connected to the broker BEFORE you start this script.
#
# Usage:   .\GO_DRYRUN_V23.ps1

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

Write-Host "================================================================" -ForegroundColor Cyan
Write-Host "  v23 DRY-RUN   (4-pair ORB + Merton-GZ + news + 4pct DD rails)" -ForegroundColor Cyan
Write-Host "  strategy has ZERO real-order side effects in this mode." -ForegroundColor Cyan
Write-Host "================================================================" -ForegroundColor Cyan

# Sanity: require Python on PATH
$py = (Get-Command python -ErrorAction SilentlyContinue).Path
if (-not $py) { Write-Error "python not on PATH"; exit 1 }

# Sanity: require numpy installed (proxy for `pip install -r requirements.txt` done)
& python -c "import numpy, pandas, scipy" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Python deps missing. Run first:" -ForegroundColor Red
    Write-Host "       pip install -r requirements.txt" -ForegroundColor Yellow
    exit 2
}

# Run the launcher - cap_mult=5.0 is the v24d-optimal sweet-spot.
& python "$PSScriptRoot\Scripts\run_v23_live.py" `
    --symbols "DE40,US30,XAUUSD,US500" `
    --risk 0.00110 `
    --cap-mult 5.0 `
    --account-kill 0.08 `
    --daily-breaker 0.02 `
    --magic 23000 `
    --news-csv "data/news/tier1_2026.csv" `
    --heartbeat 60
