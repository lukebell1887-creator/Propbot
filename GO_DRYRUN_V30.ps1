# GO_DRYRUN_V30.ps1 - start v30 in DRY-RUN mode (no real orders).
#
# v30 = v25.1 ship config recommended in Docs/V25_1_SHIP_RECOMMENDATION.md
#   * base_risk      = 0.170 %      (was 0.110 % in v23)   ★
#   * cap_mult       = 5.0          (max 0.85 % per trade)
#   * nochase cd     = 300 s        (NEW cross-symbol queue-release filter) ★
#   * DailyHalt      = 4 %          (static, prop-firm style)
#   * DDBreaker      = 4 %          (rolling peak-to-trough)
#   * SLIPPAGE LOG   = Results/v30_live_slippage.jsonl + per-heartbeat
#                      per-symbol + portfolio summary
#
# The MT5 terminal with SHF_Bridge.mq5 attached to a chart MUST be running
# and connected to the broker BEFORE you start this script.
#
# Usage:   .\GO_DRYRUN_V30.ps1

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

Write-Host "================================================================" -ForegroundColor Cyan
Write-Host "  v30 DRY-RUN  (4-pair ORB + Merton-GZ + news + 4pct DD rails)" -ForegroundColor Cyan
Write-Host "  v25.1 ship config: risk=0.170%  nochase=300s  +slippage tracker" -ForegroundColor Cyan
Write-Host "  Strategy has ZERO real-order side effects in this mode." -ForegroundColor Cyan
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

# Broker-name mapping for this 5ers FivePercentOnline-Real server,
# discovered via Scripts\probe_broker_symbols.py on 2026-04-23.
# If you move to a different broker, re-run the probe and update this line.
$BrokerNames = "DE40=DAX40,US30=US30,US500=SP500,XAUUSD=XAUUSD"

& python "$PSScriptRoot\Scripts\run_v30_live.py" `
    --symbols "DE40,US30,XAUUSD,US500" `
    --broker-names $BrokerNames `
    --risk 0.00170 `
    --cap-mult 5.0 `
    --nochase-cooldown 300 `
    --account-kill 0.08 `
    --daily-breaker 0.02 `
    --magic 30000 `
    --news-csv "data/news/tier1_2026.csv" `
    --heartbeat 60
