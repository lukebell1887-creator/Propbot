# ======================================================================
#  STOP_BOT.ps1  -  graceful kill for Python + optional MT5
#
#  1. Sends close to any python running run_v15_live.py
#  2. If that is ignored, force-kills python
#  3. Does NOT close MT5 by default (open positions keep broker-held
#     SL/TP intact). Pass -AlsoMT5 to close MT5 too.
#
#  Usage:
#     .\STOP_BOT.ps1                (stop python only)
#     .\STOP_BOT.ps1 -AlsoMT5       (stop python AND MT5)
# ======================================================================
param(
    [switch]$AlsoMT5
)

$ErrorActionPreference = "Continue"

Write-Host ""
Write-Host "======================================================================" -ForegroundColor Red
Write-Host "  STOPPING v15 bot"  -ForegroundColor Red
Write-Host "======================================================================" -ForegroundColor Red
Write-Host ""

$pyProcs = Get-Process python -ErrorAction SilentlyContinue
if ($pyProcs) {
    Write-Host ("[1/3] Asking {0} python process(es) to close cleanly ..." -f $pyProcs.Count) -ForegroundColor Cyan
    foreach ($p in $pyProcs) {
        try { $p.CloseMainWindow() | Out-Null } catch {}
    }
    Start-Sleep -Seconds 5
} else {
    Write-Host "[1/3] No python processes running - nothing to stop." -ForegroundColor Green
}

$still = Get-Process python -ErrorAction SilentlyContinue
if ($still) {
    Write-Host "[2/3] Force-killing remaining python process(es) ..." -ForegroundColor Yellow
    $still | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
} else {
    Write-Host "[2/3] All python processes stopped cleanly." -ForegroundColor Green
}

if ($AlsoMT5) {
    Write-Host "[3/3] -AlsoMT5 passed - stopping MT5 ..." -ForegroundColor Cyan
    Get-Process terminal64 -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
} else {
    Write-Host "[3/3] MT5 left running (open positions keep their broker-held SL/TP)." -ForegroundColor Green
    Write-Host "       Use  .\STOP_BOT.ps1 -AlsoMT5  if you want to close MT5 too." -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "OK  Bot stopped." -ForegroundColor Green
Write-Host ""
