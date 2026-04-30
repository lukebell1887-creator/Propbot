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
$BOT = "C:\SHF"
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
Write-Host "============================================" -ForegroundColor Cyan

Start-Process powershell -ArgumentList @(
    "-NoExit",
    "-ExecutionPolicy","Bypass",
    "-File","$BOT\GO_LIVE_V30.ps1"
) -WorkingDirectory $BOT

Start-Sleep -Seconds 8
Write-Host ""
Write-Host "  Bot started in a separate PowerShell window." -ForegroundColor Green
Write-Host "  Tail the heartbeat with:" -ForegroundColor White
Write-Host "    Get-Content $BOT\Results\v30_live_console.out -Tail 60 -Wait" -ForegroundColor Yellow
Write-Host ""
Write-Host "  Send these THREE outputs back to Cline:" -ForegroundColor White
Write-Host "    git log -1 --oneline" -ForegroundColor Yellow
Write-Host "    Get-Content $BOT\Results\v30_live_console.out -Tail 80" -ForegroundColor Yellow
Write-Host "    Get-Content $BOT\Results\v30_live_console.err -Tail 40" -ForegroundColor Yellow
Write-Host ""
