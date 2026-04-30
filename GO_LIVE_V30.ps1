# GO_LIVE_V30.ps1 - start v30 in LIVE mode (REAL ORDERS).
#
# v30 = v31 ship config (was v25.1)
#   * base_risk      = 0.185 %      (v31 ship; was 0.170% in v25.1, 0.110% in v23) ★
#   * cap_mult       = 5.0          (max 0.85 % per trade)
#   * nochase cd     = 300 s        (NEW cross-symbol queue-release filter) ★
#   * DailyHalt      = 4 %          (static, prop-firm style)
#   * DDBreaker      = 4 %          (rolling peak-to-trough)
#   * SLIPPAGE LOG   = Results/v30_live_slippage.jsonl + per-heartbeat
#                      per-symbol + portfolio summary
#
# READ TWICE BEFORE RUNNING. This places REAL orders on the connected
# broker via SHF_Bridge.mq5. Make sure:
#   1. Dry-run completed >= 1 trading day cleanly
#   2. The broker symbol mapping below is correct for your account
#   3. The MT5 terminal with SHF_Bridge.mq5 attached is running
#   4. Any older v23 / v18 / v15 launchers are STOPPED (you cannot run
#      two bots against the same broker login)
#
# Usage:   .\GO_LIVE_V30.ps1

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

Write-Host "================================================================" -ForegroundColor Yellow
Write-Host "  v30 LIVE   (REAL ORDERS)" -ForegroundColor Yellow
Write-Host "  v31 ship config: risk=0.185%  nochase=300s  +slippage tracker" -ForegroundColor Yellow
Write-Host "================================================================" -ForegroundColor Yellow

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

# ----------------------------------------------------------------------------
# v30.3 PREFLIGHT GATE — runs the live-engine contract verifier before the
# bot ever starts. Verifies imports, config defaults, ORB anchors, TP/SL
# math, partial-ladder simulation, ATR readiness and the 50-test parity net.
# Aborts the launcher if anything fails. See Scripts\preflight_v30.py.
# ----------------------------------------------------------------------------
Write-Host "Running v30.3 preflight (live-engine contract verifier)..." -ForegroundColor Yellow
$env:PYTHONIOENCODING = "utf-8"
& python "$PSScriptRoot\Scripts\preflight_v30.py"
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "ABORT: preflight failed. Bot was NOT started. See report above." -ForegroundColor Red
    Write-Host "       Fix the failing checks and re-run .\GO_LIVE_V30.ps1" -ForegroundColor Yellow
    exit 3
}
Write-Host ""

# Broker-name mapping for this 5ers FivePercentOnline-Real server,
# discovered via Scripts\probe_broker_symbols.py on 2026-04-23.
# If you move to a different broker, re-run the probe and update this line.
$BrokerNames = "DE40=DAX40,US30=US30,US500=SP500,XAUUSD=XAUUSD"

& python "$PSScriptRoot\Scripts\run_v30_live.py" `
    --live `
    --symbols "DE40,US30,XAUUSD,US500" `
    --broker-names $BrokerNames `
    --risk 0.00185 `
    --cap-mult 5.0 `
    --nochase-cooldown 300 `
    --account-kill 0.08 `
    --daily-breaker 0.02 `
    --magic 30000 `
    --news-csv "data/news/tier1_2026.csv" `
    --heartbeat 60
