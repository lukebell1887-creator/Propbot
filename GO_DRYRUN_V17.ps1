# ======================================================================
#  GO_DRYRUN_V17.ps1  -  identical to GO_LIVE_V17 but --live is OFF.
#  Use this for the 24-48 h pre-live soak test on the VPS.
# ======================================================================
$ErrorActionPreference = "Stop"
Set-Location "C:\PropBot"

Write-Host ""
Write-Host "======================================================================" -ForegroundColor Yellow
Write-Host "  DRY-RUN  v17  -  engine decisions only, NO orders will be placed"     -ForegroundColor Yellow
Write-Host "======================================================================" -ForegroundColor Yellow
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
Start-Sleep -Seconds 20

Write-Host ""
Write-Host "[4/5] Activating .venv ..." -ForegroundColor Cyan
& "C:\PropBot\.venv\Scripts\Activate.ps1"

Write-Host ""
Write-Host "[5/5] Starting v17 DRY-RUN ..." -ForegroundColor Yellow
python Scripts\run_v16_live.py `
    --risk-scale   1.0 `
    --warmup-bars  5000 `
    --heartbeat-sec 60.0 `
    --warmup-sizer-from "Results/v17_final_100000_3m_trades.json"
