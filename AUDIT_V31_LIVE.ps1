# =====================================================================
#  AUDIT_V31_LIVE.ps1
# ---------------------------------------------------------------------
#  ONE-CLICK live-bot audit.
#
#  Use case (what you asked for):
#    Backtest made +28k in 3 months, live has LOST 4.5k in 2 weeks,
#    every live trade is risking only ~$10 instead of the ~$185
#    base_risk_pct * 100k = expected risk_$.  Need to know if the
#    bot is wired the same as the backtest.
#
#  This script will:
#    (1) (optional) pull  Results/v30_live_*  +  Results/v30_state/*
#        back from the VPS so we can audit them locally, OR run on
#        the VPS directly if you're already there.
#    (2) run  Scripts/diag_v31_live_vs_backtest.py  which prints a
#        9-section PhD-grade wiring + sizing report.
#
#  USAGE
#  =====
#    Local (after copying  Results/  from VPS by hand):
#        .\AUDIT_V31_LIVE.ps1
#
#    Local + auto-pull from VPS over SCP:
#        .\AUDIT_V31_LIVE.ps1 -VpsUser Administrator -VpsHost 1.2.3.4
#
#    On the VPS itself (no SCP needed):
#        .\AUDIT_V31_LIVE.ps1 -NoPull
#
#    Only audit entries on/after a date:
#        .\AUDIT_V31_LIVE.ps1 -Since 2026-05-14
#
#    Interactive (no args -> prompts for VPS host):
#        .\AUDIT_V31_LIVE.ps1
#
# =====================================================================
[CmdletBinding()]
param(
    [string] $VpsUser = "",
    [string] $VpsHost = "",
    [string] $VpsRepo = "C:/Users/Administrator/PropBot",
    [string] $Since   = "",
    [switch] $NoPull,
    [switch] $LocalOnly
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSCommandPath
Set-Location $repo

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  v31 LIVE-BOT AUDIT" -ForegroundColor Cyan
Write-Host "  repo: $repo" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# ---------------------------------------------------------------------
# Interactive prompt if neither -NoPull nor -LocalOnly nor -VpsHost was given.
# This avoids the PowerShell "<...>" parsing trap from the README examples.
# ---------------------------------------------------------------------
if (-not $NoPull -and -not $LocalOnly -and -not $VpsHost) {
    Write-Host ""
    Write-Host "  No -VpsHost provided. Choose one:" -ForegroundColor Yellow
    Write-Host "    [1] Pull live state from VPS over SCP   (will prompt for host)"
    Write-Host "    [2] Use whatever is already in  .\Results\  (no pull)"
    Write-Host "    [3] Cancel"
    $choice = Read-Host "  Enter 1, 2 or 3"
    switch ($choice) {
        "1" {
            $VpsHost = Read-Host "  VPS IP or DNS name (e.g. 203.0.113.42)"
            if (-not $VpsUser) {
                $defaultUser = "Administrator"
                $u = Read-Host "  VPS username (Enter for '$defaultUser')"
                if ([string]::IsNullOrWhiteSpace($u)) { $VpsUser = $defaultUser } else { $VpsUser = $u }
            }
        }
        "2" { $LocalOnly = $true }
        default {
            Write-Host "  Cancelled." -ForegroundColor DarkYellow
            exit 0
        }
    }
}

# ---------------------------------------------------------------------
# (1) Pull from VPS via SCP (skip with -NoPull / -LocalOnly)
# ---------------------------------------------------------------------
if (-not $NoPull -and -not $LocalOnly -and $VpsUser -and $VpsHost) {
    $remote = "$VpsUser@${VpsHost}:$VpsRepo"
    Write-Host ""
    Write-Host "[1/2] Pulling live state from $remote ..." -ForegroundColor Yellow

    New-Item -ItemType Directory -Force -Path "Results"            | Out-Null
    New-Item -ItemType Directory -Force -Path "Results/v30_state"  | Out-Null

    $files = @(
        "Results/v30_live_trades.jsonl",
        "Results/v30_live_events.log",
        "Results/v30_live_slippage.jsonl",
        "Results/v30_state/sizer_mertongz.json",
        "Results/v30_state/dd_breaker.json",
        "Results/heartbeat_v30.json"
    )
    foreach ($f in $files) {
        $src = "$remote/$f"
        $dst = Join-Path $repo $f
        Write-Host "      scp  $src" -ForegroundColor DarkGray
        # -o BatchMode=yes will fail fast if password prompt -- swap if you use a key
        & scp -o ConnectTimeout=10 $src $dst 2>&1 | Out-Null
        if (Test-Path $dst) {
            Write-Host "      ok   -> $dst" -ForegroundColor Green
        } else {
            Write-Host "      MISS -> $f  (will be reported as missing)" -ForegroundColor DarkYellow
        }
    }
} elseif (-not $NoPull) {
    Write-Host ""
    Write-Host "[1/2] -VpsUser / -VpsHost not provided." -ForegroundColor Yellow
    Write-Host "      Skipping SCP pull. Assuming  Results/  is already populated." -ForegroundColor Yellow
} else {
    Write-Host ""
    Write-Host "[1/2] -NoPull set -- using local  Results/  as-is." -ForegroundColor Yellow
}

# ---------------------------------------------------------------------
# (2) Run the diagnostic
# ---------------------------------------------------------------------
Write-Host ""
Write-Host "[2/2] Running diag_v31_live_vs_backtest.py ..." -ForegroundColor Yellow
Write-Host ""

$pyArgs = @("Scripts/diag_v31_live_vs_backtest.py")
if ($Since) { $pyArgs += @("--since", $Since) }

& python @pyArgs
$code = $LASTEXITCODE

Write-Host ""
if ($code -eq 0) {
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host "  AUDIT PASSED -- live bot is wired the same as the backtest." -ForegroundColor Green
    Write-Host "============================================================" -ForegroundColor Green
} else {
    Write-Host "============================================================" -ForegroundColor Red
    Write-Host "  AUDIT FOUND ISSUES -- see the FINAL VERDICT block above." -ForegroundColor Red
    Write-Host "  Most common fixes:" -ForegroundColor Red
    Write-Host "    * sizer cold-started -> seed it from Results/v30_fresh_trades.json" -ForegroundColor Red
    Write-Host "    * DD breaker tripped -> check Results/v30_state/dd_breaker.json" -ForegroundColor Red
    Write-Host "    * config drift       -> rerun Scripts/verify_v31_live_wiring.py" -ForegroundColor Red
    Write-Host "============================================================" -ForegroundColor Red
}
exit $code
