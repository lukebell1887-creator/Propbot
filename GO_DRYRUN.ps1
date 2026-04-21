# ======================================================================
#  GO_DRYRUN.ps1  -  safety-test run (NO real orders will be placed)
#
#  Identical to GO_LIVE.ps1 but passes NO --live flag, so the engine
#  only logs decisions, never sends them to the broker.
#
#  Use this to prove the plumbing works before you switch to money.
# ======================================================================
$ErrorActionPreference = "Stop"
Set-Location "C:\PropBot"

Write-Host ""
Write-Host "======================================================================" -ForegroundColor Yellow
Write-Host "  DRY-RUN  -  decisions logged only, NO real orders will be placed"  -ForegroundColor Yellow
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
Write-Host "       waiting 20 s for MT5 login + EA attach ..."
Start-Sleep -Seconds 20

Write-Host ""
Write-Host "[4/5] Activating .venv ..." -ForegroundColor Cyan
& "C:\PropBot\.venv\Scripts\Activate.ps1"

Write-Host ""
Write-Host "[5/5] Starting engine in DRY-RUN - pre-flight running ..." -ForegroundColor Yellow
Write-Host "      (Ctrl-C here or run .\STOP_BOT.ps1 to halt)" -ForegroundColor Yellow
Write-Host ""

python Scripts\run_v15_live.py
