# ======================================================================
#  GO_DRYRUN_V16.ps1  -  v16 SmartBB in DRY-RUN on 5%ers MTB
#
#  Same as GO_LIVE_V16.ps1 but passes NO --live flag, so the engine decides
#  and logs signals WITHOUT placing real orders.  Perfect for 24-72h of
#  side-by-side comparison against the live v15.
#
#  .\GO_DRYRUN_V16.ps1                 # default Phase-B risk
#  .\GO_DRYRUN_V16.ps1 -NoSizer        # calendar only, fixed sizing
#  .\GO_DRYRUN_V16.ps1 -NoCalendar     # sizer only
# ======================================================================
param(
    [double]$Risk = 0.5,
    [switch]$NoSizer,
    [switch]$NoCalendar
)

$ErrorActionPreference = "Stop"
Set-Location "C:\PropBot"

$sizerTxt = if ($NoSizer)    { "OFF" } else { "ON" }
$calTxt   = if ($NoCalendar) { "OFF" } else { "ON" }

Write-Host ""
Write-Host "======================================================================" -ForegroundColor Yellow
Write-Host "  DRY-RUN V16  -  risk_scale = $Risk  sizer=$sizerTxt  calendar=$calTxt" -ForegroundColor Yellow
Write-Host "  NO real orders will be placed.  Decisions logged only."                -ForegroundColor Yellow
Write-Host "======================================================================" -ForegroundColor Yellow
Write-Host ""

Write-Host "[1/4] Stopping any previous python ..." -ForegroundColor Cyan
Get-Process | Where-Object { $_.Name -eq "python" } |
    Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

Write-Host "[2/4] git pull ..." -ForegroundColor Cyan
git pull

Write-Host "[3/4] Activating .venv ..." -ForegroundColor Cyan
& "C:\PropBot\.venv\Scripts\Activate.ps1"

Write-Host "[4/4] Starting v16 in DRY-RUN ..." -ForegroundColor Yellow
$pyArgs = @("Scripts\run_v16_live.py", "--risk-scale", $Risk)
if ($NoSizer)    { $pyArgs += "--no-sizer" }
if ($NoCalendar) { $pyArgs += "--no-calendar" }
python @pyArgs
