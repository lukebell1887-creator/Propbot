# ======================================================================
#  RESTART_WITH_V313_FIX.ps1
#
#  One-shot restart of the v30/v31 live bot after pulling the v31.3
#  sizer fix (ewma_alpha 0.20 -> 0.05, min_risk_pct 0 -> 0.05%).
#
#  What it does:
#     1. Stops any running python (bot) - leaves MT5 running so any
#        open positions keep their broker-side SL/TP intact
#     2. git pull              (gets the v31.3 fix from origin/main)
#     3. Verifies the new defaults are loaded in V30LiveConfig
#     4. DELETES Results\v30_state\sizer_mertongz.json so the bot
#        re-seeds from the Jan-Apr backtest ledger on next start
#        (otherwise the saved alpha=0.20 state would be loaded and
#         the bot would refuse it - this just makes the audit trail
#         visible)
#     5. Starts the bot via the existing GO_LIVE_V30.ps1 entry point
#
#  Run on the VPS PowerShell, from C:\PropBot:
#      .\RESTART_WITH_V313_FIX.ps1
#
#  If you want a paper-trade dry-run first instead, pass -DryRun:
#      .\RESTART_WITH_V313_FIX.ps1 -DryRun
# ======================================================================
param(
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
Set-Location "C:\PropBot"

Write-Host ""
Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host "  v31.3 RESTART  -  slow EWMA + 0.05% risk floor"                       -ForegroundColor Cyan
if ($DryRun) {
Write-Host "  Mode: DRY-RUN (paper trading, no real orders)"                       -ForegroundColor Yellow
} else {
Write-Host "  Mode: LIVE (real money)"                                             -ForegroundColor Green
}
Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host ""

# ---------------------------------------------------------------------
# 1. Stop existing bot (Python only; leave MT5 + open positions alone)
# ---------------------------------------------------------------------
Write-Host "[1/5] Stopping existing bot ..." -ForegroundColor Cyan
$py = Get-Process python -ErrorAction SilentlyContinue
if ($py) {
    Write-Host ("       killing {0} python process(es)" -f $py.Count)
    $py | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
} else {
    Write-Host "       no python running"
}

# ---------------------------------------------------------------------
# 2. git pull
# ---------------------------------------------------------------------
Write-Host ""
Write-Host "[2/5] git pull ..." -ForegroundColor Cyan
git pull
if ($LASTEXITCODE -ne 0) { throw "git pull failed - aborting restart" }

# ---------------------------------------------------------------------
# 3. Verify the v31.3 fix actually loaded
# ---------------------------------------------------------------------
Write-Host ""
Write-Host "[3/5] Verifying v31.3 defaults are present ..." -ForegroundColor Cyan
& "C:\PropBot\.venv\Scripts\Activate.ps1"
$check = python -c "from src.live.v30_live import V30LiveConfig; v=V30LiveConfig(); print(f'{v.ewma_alpha} {v.min_risk_pct}')"
Write-Host ("       runtime: ewma_alpha={0}" -f $check)
if ($check -notmatch "^0\.05 0\.0005") {
    Write-Host "  *** ERROR: expected '0.05 0.0005' but got '$check' ***" -ForegroundColor Red
    Write-Host "  *** v31.3 fix did NOT reach this VPS.  Did the pull succeed? ***" -ForegroundColor Red
    throw "v31.3 verification failed"
}
Write-Host "       v31.3 verified OK" -ForegroundColor Green

# ---------------------------------------------------------------------
# 4. Clear stale sizer state so we re-seed cleanly from Jan-Apr backtest
# ---------------------------------------------------------------------
Write-Host ""
Write-Host "[4/5] Clearing stale sizer state (forces re-seed from Jan-Apr) ..." -ForegroundColor Cyan
$stateFile = "Results\v30_state\sizer_mertongz.json"
if (Test-Path $stateFile) {
    $backup = "$stateFile.before_v313.bak"
    Copy-Item $stateFile $backup -Force
    Remove-Item $stateFile -Force
    Write-Host ("       deleted {0} (backup at {1})" -f $stateFile, $backup)
} else {
    Write-Host ("       {0} not present - nothing to clear" -f $stateFile)
}

# ---------------------------------------------------------------------
# 5. Start bot
# ---------------------------------------------------------------------
Write-Host ""
if ($DryRun) {
    Write-Host "[5/5] Starting DRY-RUN ..." -ForegroundColor Yellow
    Write-Host ""
    if (Test-Path ".\GO_DRYRUN_V30.ps1") {
        & .\GO_DRYRUN_V30.ps1
    } else {
        python Scripts\run_v30_live.py --dry-run
    }
} else {
    Write-Host "[5/5] Starting LIVE ..." -ForegroundColor Green
    Write-Host ""
    if (Test-Path ".\PULL_AND_GO_LIVE_V30.ps1") {
        # PULL_AND_GO_LIVE_V30 already does pull+launch; we've already pulled,
        # so use GO_LIVE_V30 directly if that's the simpler entrypoint.
        if (Test-Path ".\GO_LIVE_V30.ps1") {
            & .\GO_LIVE_V30.ps1
        } else {
            & .\PULL_AND_GO_LIVE_V30.ps1
        }
    } elseif (Test-Path ".\GO_LIVE_V30.ps1") {
        & .\GO_LIVE_V30.ps1
    } else {
        # last resort - direct python launch
        python Scripts\run_v30_live.py --live
    }
}
