# ======================================================================
#  GO_DRYRUN_V18.ps1  -  v18 in DRY-RUN mode.  Same code path as live,
#  but no orders are sent.  Use for the 24-48 h VPS soak before flipping.
# ======================================================================
$ErrorActionPreference = "Stop"
Set-Location "C:\PropBot"

Write-Host ""
Write-Host "======================================================================" -ForegroundColor Yellow
Write-Host "  DRY-RUN  v18  -  decisions only, NO orders placed"                    -ForegroundColor Yellow
Write-Host "======================================================================" -ForegroundColor Yellow
Write-Host ""

Get-Process | Where-Object { $_.Name -match "^(terminal64|python)$" } |
    Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3
git pull

$mt5 = Get-ChildItem -Path "C:\Program Files","C:\Program Files (x86)" -Recurse -Filter terminal64.exe -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $mt5) { throw "MT5 not found" }
Start-Process $mt5.FullName
Start-Sleep -Seconds 20

& "C:\PropBot\.venv\Scripts\Activate.ps1"

python Scripts\run_v18_live.py
