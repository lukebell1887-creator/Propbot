# =============================================================================
# PULL_AND_GO_LIVE_V30.ps1
# -----------------------------------------------------------------------------
# Run this on the VPS:  .\PULL_AND_GO_LIVE_V30.ps1
#
# What it does:
#   1. Stops any python running run_v30_live.py
#   2. git fetch + git reset --hard origin/main   (pulls latest)
#   3. pip install -r requirements.txt            (idempotent, includes MetaTrader5)
#   4. Probes the broker for live tick_value per symbol (DAX40 EUR->USD fix)
#       - prints the BROKER PIP-VALUE TABLE to the console
#       - aborts launch if MT5 isn't reachable
#   5. Runs the 29 live<->backtest parity unit tests
#       - aborts launch if any test fails
#   6. Launches GO_LIVE_V30.ps1 in a NEW PowerShell window (real orders)
#
# Expected commit after pull: f963e65 (or newer; v30.4 broker pip-value fix)

# =============================================================================
$ErrorActionPreference = "Stop"
# Auto-detect bot location: prefer C:\PropBot, fall back to C:\SHF, else CWD
if     (Test-Path "C:\PropBot\GO_LIVE_V30.ps1") { $BOT = "C:\PropBot" }
elseif (Test-Path "C:\SHF\GO_LIVE_V30.ps1")     { $BOT = "C:\SHF"     }
else                                             { $BOT = (Get-Location).Path }
Write-Host ("  Using bot directory: {0}" -f $BOT) -ForegroundColor Magenta
Set-Location -LiteralPath $BOT


Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  STEP 1/6  Stopping any running v30 bot" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan

Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match "run_v30_live" } |
    ForEach-Object {
        Write-Host ("  Stopping PID {0}" -f $_.ProcessId) -ForegroundColor Yellow
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
Start-Sleep -Seconds 2
Write-Host "  All v30 processes stopped." -ForegroundColor Green

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  STEP 2/6  Pulling latest from GitHub" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
git fetch --all
git reset --hard origin/main
git log -1 --oneline
Write-Host ""
Write-Host "  Expected commit: f963e65 (v30 parity) or newer  (v30.4 = broker pip-value fix)" -ForegroundColor Yellow

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  STEP 3/6  Verifying Python deps" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
python -m pip install -q -r requirements.txt
python -c "import numpy, pandas, scipy; print('  deps OK')"
# v30.4 — MetaTrader5 python package is required for the broker-truth
# tick_value fetch. Without it the engine silently falls back to
# hardcoded $1/pt and DAX40 is under-sized by ~14%.
python -c "import MetaTrader5; print('  MetaTrader5 pkg OK  (version ' + MetaTrader5.__version__ + ')')"
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "  ABORT: MetaTrader5 python package not importable." -ForegroundColor Red
    Write-Host "         Run: python -m pip install MetaTrader5" -ForegroundColor Yellow
    exit 1
}

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  STEP 4/6  Probing broker for tick_value per symbol" -ForegroundColor Cyan
Write-Host "  (MT5 terminal must be running and logged in)" -ForegroundColor Yellow
Write-Host "============================================" -ForegroundColor Cyan
$env:PYTHONIOENCODING = "utf-8"
# Pass the same broker-symbol overrides the live engine uses, otherwise
# the probe looks up DE40.cash / US500.cash (defaults baked into
# V30_BROKER_NAMES) instead of the actual 5ers names DAX40 / SP500.
$ProbeBrokerNames = "DE40=DAX40,US30=US30,US500=SP500,XAUUSD=XAUUSD"
python Scripts\probe_broker_pip_values.py --broker-names $ProbeBrokerNames
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "  ABORT: broker probe failed. The engine would fall back to" -ForegroundColor Red
    Write-Host "         hardcoded `$1/pt and DAX40 would be UNDER-sized." -ForegroundColor Red
    Write-Host "         Check that the MT5 terminal is running, logged in," -ForegroundColor Yellow
    Write-Host "         and 'Allow algorithmic trading' is enabled." -ForegroundColor Yellow
    exit 1
}
Write-Host "  Broker pip-values verified (live from MT5)." -ForegroundColor Green

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  STEP 5/6  Running 29 parity unit tests" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
python -m pytest tests/test_v30_live_bt_parity.py -q
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "  ABORT: parity tests failed. Bot NOT started." -ForegroundColor Red
    exit 1
}
Write-Host "  All 29 parity tests GREEN." -ForegroundColor Green

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  STEP 6/6  Starting v30 LIVE (real orders)" -ForegroundColor Cyan

Write-Host "  Heartbeats print live below.  Ctrl+C to stop." -ForegroundColor Yellow
Write-Host "  Bot also writes its own logs to Results\:" -ForegroundColor Cyan
Write-Host "    v30_live_events.log     - structured events" -ForegroundColor Cyan
Write-Host "    heartbeat_v30.json      - latest heartbeat" -ForegroundColor Cyan
Write-Host "    v30_live_trades.jsonl   - one line per trade" -ForegroundColor Cyan
Write-Host "    v30_live_telemetry.json - latest telemetry" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

# Make sure the console renders python's UTF-8 (μ̂, σ̂, ✓, ─ etc.) without mojibake.
chcp 65001 | Out-Null
$env:PYTHONIOENCODING = "utf-8"

# Run the original GO_LIVE_V30.ps1 directly in the SAME window — no pipe, no tee.
# That preserves real-time heartbeats and UTF-8 rendering exactly like the old display.
& "$BOT\GO_LIVE_V30.ps1"



