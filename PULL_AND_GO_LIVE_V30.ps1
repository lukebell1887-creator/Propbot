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
Write-Host "  Output is streamed below AND written to:" -ForegroundColor Cyan
Write-Host "    $BOT\Results\v30_live_console.out" -ForegroundColor Cyan
Write-Host "  Press Ctrl+C to stop the bot." -ForegroundColor Yellow
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

if (-not (Test-Path "$BOT\Results")) { New-Item -ItemType Directory -Path "$BOT\Results" | Out-Null }

# Run GO_LIVE_V30.ps1 in the SAME window (no Start-Process), tee everything to a file.
# 2>&1 merges stderr into stdout so a single Tee-Object captures the lot.
& "$BOT\GO_LIVE_V30.ps1" 2>&1 | Tee-Object -FilePath "$BOT\Results\v30_live_console.out"


