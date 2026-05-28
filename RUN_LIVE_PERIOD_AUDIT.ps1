# =============================================================================
#  RUN_LIVE_PERIOD_AUDIT.ps1
# =============================================================================
#
#  PURPOSE
#  -------
#  You backtested the bot on Jan 24 -> Apr 23 and it made ~$28k.  The live bot
#  has been running Apr 27 -> today (May 28) and has lost ~$4.5k.  This script
#  answers ONE question:
#
#     "If I had run the BACKTEST on the SAME 31 days the bot has been live,
#      would it have made or lost money?"
#
#  WHAT THIS DOES
#  --------------
#  1. STOPS the live bot.
#  2. Backs up the current Jan-Apr historical CSVs.
#  3. Downloads FRESH M1 bars from the 5%ers MT5 terminal for the live window
#     (April 20 -> today) to data/historical/.
#  4. Runs the EXACT v30 backtest engine (same params as the live bot) on that
#     fresh data.  Saves trades to Results/v30_live_period_trades.json.
#  5. Compares those backtest trades to your actual live trades
#     (Results/v30_live_trades.jsonl) using the same matching as the parity
#     audit.
#  6. Restores the Jan-Apr CSVs so nothing else breaks.
#
#  WHAT THE OUTPUT TELLS YOU
#  -------------------------
#  * If backtest LOSES MONEY on the live window -> strategy is broken in this
#    regime, the bot is doing its job, nothing to fix in code.
#  * If backtest MAKES MONEY but live LOST money -> wiring problem (sizing,
#    confidence, slippage, missed entries) and the parity tables show which.
#
#  USAGE
#  -----
#     cd C:\PropBot
#     .\RUN_LIVE_PERIOD_AUDIT.ps1
#
#  Run this on the VPS (which is where the live MT5 + the live trade log are).
# =============================================================================

$ErrorActionPreference = 'Continue'
$PSDefaultParameterValues['Out-File:Encoding'] = 'utf8'

$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ROOT

$STAMP = Get-Date -Format "yyyy-MM-dd_HHmmss"
$LOG   = "$ROOT\Results\live_period_audit_$STAMP.log"
$null  = New-Item -ItemType Directory -Force -Path "$ROOT\Results" | Out-Null

function Section($title) {
    $bar = "=" * 90
    Write-Host ""
    Write-Host $bar -ForegroundColor Cyan
    Write-Host "  $title" -ForegroundColor Cyan
    Write-Host $bar -ForegroundColor Cyan
    "$bar`r`n  $title`r`n$bar" | Tee-Object -FilePath $LOG -Append | Out-Null
}

function Step($n, $txt) {
    Write-Host ""
    Write-Host ">>> [$n] $txt" -ForegroundColor Yellow
    ">>> [$n] $txt" | Tee-Object -FilePath $LOG -Append | Out-Null
}

Section "LIVE PERIOD AUDIT  (log: $LOG)"
Write-Host "  ROOT : $ROOT"
Write-Host "  STAMP: $STAMP"

# -----------------------------------------------------------------------------
# STEP 1 -- Stop the live bot so nothing fights over MT5
# -----------------------------------------------------------------------------
Step 1 "Stopping the live bot (so nothing else is using MT5)"
try {
    if (Test-Path "$ROOT\STOP_BOT.ps1") {
        & "$ROOT\STOP_BOT.ps1" 2>&1 | Tee-Object -FilePath $LOG -Append
    } else {
        Get-Process -Name "python*" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match 'run_v30|run_v23|run_v18' } |
            ForEach-Object {
                Write-Host "    killing python pid=$($_.Id)"
                Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
            }
    }
    Start-Sleep -Seconds 3
} catch {
    Write-Host "    WARN: stop step failed, continuing anyway: $_" -ForegroundColor DarkYellow
}

# -----------------------------------------------------------------------------
# STEP 2 -- Download fresh data for the live window
# -----------------------------------------------------------------------------
Step 2 "Downloading M1 bars for the LIVE window (Apr 20 -> today)"
Write-Host "    (this backs up the Jan-Apr CSVs FIRST, then overwrites)"

# Find earliest entry in live log so we cover the whole live period
$liveLog = "$ROOT\Results\v30_live_trades.jsonl"
$startArg = "2026-04-20"
if (Test-Path $liveLog) {
    try {
        $firstLine = Get-Content $liveLog -TotalCount 200 |
                     Where-Object { $_ -match '"event":\s*"ENTRY"' } |
                     Select-Object -First 1
        if ($firstLine) {
            $obj = $firstLine | ConvertFrom-Json
            $ts  = $obj.ts_utc
            if (-not $ts) { $ts = $obj.ts }
            if ($ts) {
                $startArg = (Get-Date $ts).AddDays(-2).ToString("yyyy-MM-dd")
                Write-Host "    auto-detected window start from live log: $startArg"
            }
        }
    } catch {}
}
$endArg = (Get-Date).ToString("yyyy-MM-dd")

Write-Host "    window: $startArg -> $endArg"
python "$ROOT\Scripts\download_live_period.py" $startArg $endArg 2>&1 |
    Tee-Object -FilePath $LOG -Append
$dlExit = $LASTEXITCODE

if ($dlExit -ne 0) {
    Write-Host ""
    Write-Host "!!! DOWNLOAD FAILED (exit=$dlExit) -- aborting before backtest" -ForegroundColor Red
    Write-Host "    Make sure the 5%ers MT5 terminal is OPEN and LOGGED IN." -ForegroundColor Red
    Write-Host "    Trying to restore Jan-Apr CSVs..." -ForegroundColor Red
    if (Test-Path "$ROOT\data\historical_backup_pre_liveperiod") {
        Copy-Item -Force -Recurse "$ROOT\data\historical_backup_pre_liveperiod\*.csv" "$ROOT\data\historical\"
        Write-Host "    [OK] restored Jan-Apr CSVs"
    }
    exit 2
}

# -----------------------------------------------------------------------------
# STEP 3 -- Run the v30 backtest engine on the new data
# -----------------------------------------------------------------------------
Step 3 "Running v30 BACKTEST on the live-period bars"
python "$ROOT\Scripts\run_v30_backtest_live_period.py" 2>&1 |
    Tee-Object -FilePath $LOG -Append
$btExit = $LASTEXITCODE

# -----------------------------------------------------------------------------
# STEP 4 -- Compare backtest trades to actual live trades
# -----------------------------------------------------------------------------
Step 4 "Comparing live trades vs backtest trades (parity audit)"
if (-not (Test-Path "$ROOT\Results\v30_live_period_trades.json")) {
    Write-Host "    !! backtest output missing -- skipping parity" -ForegroundColor Red
} else {
    python "$ROOT\Scripts\parity_live_vs_backtest_window.py" `
        "$ROOT\Results\v30_live_period_trades.json" `
        "$ROOT\Results\v30_live_trades.jsonl" 2>&1 |
        Tee-Object -FilePath $LOG -Append
}

# -----------------------------------------------------------------------------
# STEP 5 -- Daily PnL distribution from the live log (sanity check)
# -----------------------------------------------------------------------------
Step 5 "Live daily PnL breakdown (what the bot actually did)"
if (Test-Path "$ROOT\Scripts\daily_pnl_breakdown.py") {
    python "$ROOT\Scripts\daily_pnl_breakdown.py" 2>&1 | Tee-Object -FilePath $LOG -Append
}

# -----------------------------------------------------------------------------
# STEP 6 -- Restore the Jan-Apr CSVs
# -----------------------------------------------------------------------------
Step 6 "Restoring the Jan-Apr backtest CSVs so other scripts still work"
if (Test-Path "$ROOT\data\historical_backup_pre_liveperiod") {
    Copy-Item -Force "$ROOT\data\historical_backup_pre_liveperiod\*.csv" `
                     "$ROOT\data\historical\" -ErrorAction SilentlyContinue
    Write-Host "    [OK] restored Jan-Apr CSVs from backup" -ForegroundColor Green
} else {
    Write-Host "    no backup found -- leaving live-period CSVs in place"
}

# -----------------------------------------------------------------------------
# DONE
# -----------------------------------------------------------------------------
Section "AUDIT COMPLETE"
Write-Host "  Log file        : $LOG"
Write-Host "  Backtest trades : $ROOT\Results\v30_live_period_trades.json"
Write-Host "  Live trades     : $ROOT\Results\v30_live_trades.jsonl"
Write-Host ""
Write-Host "  HOW TO READ THE OUTPUT"           -ForegroundColor Cyan
Write-Host "  ----------------------"           -ForegroundColor Cyan
Write-Host "   1.  Look at the 'V30 BACKTEST' headline section first:"
Write-Host "         net PnL > 0  -> strategy is profitable in this regime"
Write-Host "         net PnL < 0  -> strategy itself is bleeding -- live bot is doing its job"
Write-Host ""
Write-Host "   2.  Then the 'PARITY' section:"
Write-Host "         miss rate < 5 %    + extra rate < 5 %     -> wiring is CORRECT"
Write-Host "         miss rate > 30 %   or extra rate > 30 %   -> live is OUT OF SYNC with backtest"
Write-Host ""
Write-Host "   3.  Check 'BY DAY' table -- if backtest PnL is heavily positive on the"
Write-Host "       days you lost, those are the trades the live bot MISSED (wiring bug)."
Write-Host ""
Write-Host "  To restart the live bot:  .\GO_LIVE.ps1"
Write-Host ""
