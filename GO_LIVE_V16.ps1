# ======================================================================
#  GO_LIVE_V16.ps1  -  one-click LIVE trading (v16 = v15 + Dynamic Kelly + Calendar)
#
#  Default: HALF risk (-Risk 0.5)  -  Phase B
#  Override: .\GO_LIVE_V16.ps1 -Risk 1.0    (full size - Phase C)
#
#  v16 adds on top of v15:
#    * Thorp-Kelly dynamic sizer (Kelly x DD throttle x vol target x regime)
#    * TradingCalendar (weekend / rollover 20:58-22:02 UTC / holiday blackouts)
#
#  To run WITHOUT the dynamic sizer (safer first switch):
#    .\GO_LIVE_V16.ps1 -Risk 0.5 -NoSizer
#
#  To run WITHOUT the calendar (not recommended):
#    .\GO_LIVE_V16.ps1 -Risk 0.5 -NoCalendar
#
#  Stop with Ctrl-C or run .\STOP_BOT.ps1
# ======================================================================
param(
    [double]$Risk = 0.5,
    [switch]$NoSizer,
    [switch]$NoCalendar,
    [int]$WarmupBars = 5000,
    [double]$HeartbeatSec = 60.0
)


$ErrorActionPreference = "Stop"
Set-Location "C:\PropBot"

$sizerTxt = if ($NoSizer)    { "OFF (v14 fixed sizing)" } else { "ON  (Thorp-Kelly)" }
$calTxt   = if ($NoCalendar) { "OFF (no blackouts)"     } else { "ON  (weekend/rollover/holiday)" }

Write-Host ""
Write-Host "======================================================================" -ForegroundColor Green
Write-Host "  GO LIVE  V16  -  risk_scale = $Risk"                                   -ForegroundColor Green
Write-Host "  Dynamic sizer : $sizerTxt"                                              -ForegroundColor Green
Write-Host "  Calendar      : $calTxt"                                                -ForegroundColor Green
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
Write-Host "[5/5] Starting v16 engine in LIVE mode - pre-flight running ..." -ForegroundColor Yellow
Write-Host "      (Ctrl-C here or run .\STOP_BOT.ps1 to halt)" -ForegroundColor Yellow
Write-Host ""

$pyArgs = @(
    "Scripts\run_v16_live.py", "--live",
    "--risk-scale", $Risk,
    "--warmup-bars", $WarmupBars,
    "--heartbeat-sec", $HeartbeatSec
)
if ($NoSizer)    { $pyArgs += "--no-sizer" }
if ($NoCalendar) { $pyArgs += "--no-calendar" }

Write-Host ("       warmup-bars = {0}   heartbeat-sec = {1}" -f $WarmupBars, $HeartbeatSec)
Write-Host ""

python @pyArgs


