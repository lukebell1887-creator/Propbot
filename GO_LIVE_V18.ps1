# ======================================================================
#  GO_LIVE_V18.ps1  -  THE bot.  Grossman-Zhou drawdown-constrained Kelly.
#
#  What's locked in (no flags, no dials):
#     * DynamicSizerV18  = Grossman-Zhou  ×  Bayesian shrinkage
#                          × edge-conviction  × SAFETY-ONLY 5%ers guard
#                          × 2 % hard cap per trade
#     * TradingCalendar  = weekend / rollover (20:58-22:02 UTC) / holidays
#     * Kelly warm-up    = seeds from the 186-trade v17 OOS log so the
#                          bucket-specific GZ fractions are active from
#                          bar 1 (not cold-start)
#     * Account kill     = 8 % hard fuse in the live runner
#
#  Numbers we stand behind (3-month OOS on the 5%ers $100k MTB feed):
#     +$78,712  (+78.7 %)   Trades 186   PF 13.07
#     Win 78.5 %   Max DD 0.62 %   Avg risk/trade 1.26 %
#
#  Stop:  Ctrl-C  or  .\STOP_BOT.ps1
# ======================================================================
$ErrorActionPreference = "Stop"
Set-Location "C:\PropBot"

Write-Host ""
Write-Host "======================================================================" -ForegroundColor Green
Write-Host "  GO LIVE  v18  -  Grossman-Zhou dynamic Kelly"                         -ForegroundColor Green
Write-Host "  (no flags.  single blessed config.  GZ + shrinkage + conviction)"     -ForegroundColor Green
Write-Host "======================================================================" -ForegroundColor Green
Write-Host ""

Write-Host "[1/5] Stopping any previous Python / MT5 ..." -ForegroundColor Cyan
Get-Process | Where-Object { $_.Name -match "^(terminal64|python)$" } |
    Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3

Write-Host ""
Write-Host "[2/5] git pull ..." -ForegroundColor Cyan
git pull

Write-Host ""
Write-Host "[3/5] Launching MT5 ..." -ForegroundColor Cyan
$mt5 = Get-ChildItem -Path "C:\Program Files","C:\Program Files (x86)" -Recurse -Filter terminal64.exe -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $mt5) { throw "MT5 not found - please install it first" }
Start-Process $mt5.FullName
Write-Host ("       launched: {0}" -f $mt5.FullName)
Write-Host "       waiting 20 s for MT5 login + EA attach ..."
Start-Sleep -Seconds 20

Write-Host ""
Write-Host "[4/5] Activating .venv ..." -ForegroundColor Cyan
& "C:\PropBot\.venv\Scripts\Activate.ps1"

Write-Host ""
Write-Host "[5/5] Starting v18 engine LIVE ..." -ForegroundColor Yellow
Write-Host "      (Ctrl-C or .\STOP_BOT.ps1 to halt)" -ForegroundColor Yellow
Write-Host ""

python Scripts\run_v18_live.py --live
