# =============================================================================
# PULL_AND_GO_LIVE_V30.ps1
# -----------------------------------------------------------------------------
# Run this on the VPS:  .\PULL_AND_GO_LIVE_V30.ps1
#
# What it does:
#   1. Stops any python running run_v30_live.py
#   2. git fetch + git reset --hard origin/main   (pulls latest)
#   3. pip install -r requirements.txt            (idempotent)
#   4. Runs the 29 live<->backtest parity unit tests
#       - aborts launch if any test fails
#   5. Launches GO_LIVE_V30.ps1 in a NEW PowerShell window (real orders)
#
# Expected commit after pull: f963e65 (or newer)
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
Write-Host "  STEP 1/5  Stopping any running v30 bot" -ForegroundColor Cyan
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
Write-Host "  STEP 2/5  Pulling latest from GitHub" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
git fetch --all
git reset --hard origin/main
git log -1 --oneline
Write-Host ""
Write-Host "  Expected commit: f963e65 (v30 parity) or newer" -ForegroundColor Yellow

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  STEP 3/5  Verifying Python deps" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
python -m pip install -q -r requirements.txt
python -c "import numpy, pandas, scipy; print('  deps OK')"

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  STEP 4/5  Running 29 parity unit tests" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
$env:PYTHONIOENCODING = "utf-8"
python -m pytest tests/test_v30_live_bt_parity.py -q
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "  ABORT: parity tests failed. Bot NOT started." -ForegroundColor Red
    exit 1
}
Write-Host "  All 29 parity tests GREEN." -ForegroundColor Green

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  STEP 5/5  Starting v30 LIVE (real orders)" -ForegroundColor Cyan
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



