# ======================================================================
#  GO_LIVE.ps1  -  one-click LIVE trading (v15 SmartBB on 5%ers MTB)
#
#  Default: HALF risk (0.25% per trade)  -  Phase B
#  Override: .\GO_LIVE.ps1 -Risk 1.0    (full size - Phase C)
#
#  What this does:
#    1. Kills any existing Python / MT5 (fresh start)
#    2. git pull (latest code)
#    3. Launches MT5 and waits 20 s for it to log in
#    4. Runs full pre-flight check -> prints OK/FAIL for each safety layer
#    5. Starts engine in LIVE mode with the risk scale you chose
#
#  Stop with Ctrl-C or run .\STOP_BOT.ps1
# ======================================================================
param(
    [double]$Risk = 0.5
)

$ErrorActionPreference = "Stop"
Set-Location "C:\PropBot"

Write-Host ""
Write-Host "======================================================================" -ForegroundColor Green
Write-Host "  GO LIVE  -  risk_scale = $Risk   (0.5 = half-risk, 1.0 = full-risk)"  -ForegroundColor Green
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
Write-Host "[5/5] Starting engine in LIVE mode - pre-flight running ..." -ForegroundColor Yellow
Write-Host "      (Ctrl-C here or run .\STOP_BOT.ps1 to halt)" -ForegroundColor Yellow
Write-Host ""

python Scripts\run_v15_live.py --live --risk-scale $Risk
