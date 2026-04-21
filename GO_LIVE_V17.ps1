# ======================================================================
#  GO_LIVE_V17.ps1  -  THE one-click LIVE launcher.  No flags.  No dials.
#
#  What you get (locked defaults):
#     * Dynamic Kelly sizing         ON (per symbol × side, PhD stack)
#     * Trading calendar blackouts   ON (weekend / rollover / holidays)
#     * Risk scale                   1.0 (full size - config is validated)
#     * Account kill switch          8 % account DD (hard cut)
#     * Warm-up                      5,000 M1 bars/symbol pulled from broker
#     * Kelly history warm-up        ON (seeds from latest backtest so
#                                          Kelly is live from bar 1)
#
#  Results we're standing behind (3-month OOS, 5%ers $100k MTB feed):
#     Return       +38.0 %    Trades  186    PF  6.97
#     Win rate      78.5 %    Exp.   +0.472 R   Max DD  0.77 %
#
#  Per-ticker profile (avg R / win-rate):
#     DE40 short  43 +0.263 67.4 | DE40 long   36 +0.543 88.9
#     US30 long   26 +0.307 65.4 | US30 short  19 +0.715 89.5
#     US100 long  18 +0.568 83.3 | US100 short 13 +1.239 92.3
#     US500 long  18 +0.247 83.3 | US500 short  8 +0.557 87.5
#     XAUUSD long  3 +0.119 66.7 | XAUUSD sh.   2 -0.155  0.0  (starved)
#
#  Stop: Ctrl-C  or  .\STOP_BOT.ps1
# ======================================================================
$ErrorActionPreference = "Stop"
Set-Location "C:\PropBot"

Write-Host ""
Write-Host "======================================================================" -ForegroundColor Green
Write-Host "  GO LIVE  v17 FINAL  -  dynamic Kelly x calendar x 5%ers guard"        -ForegroundColor Green
Write-Host "  (single blessed config; no -Risk/-NoSizer/-NoCalendar flags)"         -ForegroundColor Green
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
Write-Host "[5/5] Starting v17 engine in LIVE mode - pre-flight running ..." -ForegroundColor Yellow
Write-Host "      (Ctrl-C here or run .\STOP_BOT.ps1 to halt)" -ForegroundColor Yellow
Write-Host ""

# v17 = v16 runner with every knob set to its blessed default.
# Sizer + Calendar are ON (no --no-sizer / --no-calendar).
# Risk scale pinned to 1.0 (full size).
python Scripts\run_v16_live.py `
    --live `
    --risk-scale   1.0 `
    --warmup-bars  5000 `
    --heartbeat-sec 60.0 `
    --warmup-sizer-from "Results/v17_final_100000_3m_trades.json"
