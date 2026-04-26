# ======================================================================
#  STATUS.ps1  --  one-shot health check + live log tail (PropBot v30)
#
#  Shows:
#    - Whether python (engine) is running and how many processes
#    - Whether MT5 is running
#    - Current day's start_live.bat log: logs\live_YYYY-MM-DD.log
#    - Engine events log:                Results\v30_live_events.log
#    - Trade ledger:                     Results\v30_live_trades.jsonl
#    - Most recent heartbeat line so you can see the bot is alive
#    - Last 50 lines of the freshest log
#    - Slippage summary if any trades have been recorded
#
#  Pass -Tail to live-stream the freshest log (Ctrl-C to exit).
# ======================================================================
param(
    [switch]$Tail
)

$ErrorActionPreference = "Continue"
$ROOT       = "C:\PropBot"
$DAILY_LOG  = Join-Path $ROOT ("logs\live_{0}.log" -f (Get-Date -Format 'yyyy-MM-dd'))
$EVENTS_LOG = Join-Path $ROOT "Results\v30_live_events.log"
$TRADES     = Join-Path $ROOT "Results\v30_live_trades.jsonl"

# Pick freshest log: prefer today's daily wrapper log, fallback to events log
$LOG = $null
if (Test-Path $DAILY_LOG) {
    $LOG = $DAILY_LOG
    $LOG_LABEL = "today's wrapper log"
} elseif (Test-Path $EVENTS_LOG) {
    $LOG = $EVENTS_LOG
    $LOG_LABEL = "engine events log"
}

Write-Host ""
Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host ("  PropBot v30  --  STATUS  {0}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')) -ForegroundColor Cyan
Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host ""

# --------------------------------------------------------------------
# 1. processes
# --------------------------------------------------------------------
$py  = Get-Process python     -ErrorAction SilentlyContinue
$mt5 = Get-Process terminal64 -ErrorAction SilentlyContinue

if ($py) {
    $pyPids = ($py | ForEach-Object { $_.Id }) -join ','
    $ram    = [int](($py | Measure-Object WorkingSet -Sum).Sum / 1MB)
    Write-Host ("  [OK]    python engine   RUNNING   pid={0}  ram={1} MB" -f $pyPids, $ram) -ForegroundColor Green
} else {
    Write-Host "  [FAIL]  python engine   NOT running  --  use .\GO_DRYRUN_V30.ps1 or .\GO_LIVE_V30.ps1" -ForegroundColor Red
}

if ($mt5) {
    $mtPids = ($mt5 | ForEach-Object { $_.Id }) -join ','
    Write-Host ("  [OK]    MT5 terminal    RUNNING   pid={0}" -f $mtPids) -ForegroundColor Green
} else {
    Write-Host "  [FAIL]  MT5 terminal    NOT running" -ForegroundColor Red
}

# --------------------------------------------------------------------
# 2. log freshness
# --------------------------------------------------------------------
Write-Host ""
if ($LOG -and (Test-Path $LOG)) {
    $lf = Get-Item $LOG
    $ageSec = [int]((Get-Date) - $lf.LastWriteTime).TotalSeconds
    Write-Host ("  Active log ({0}):" -f $LOG_LABEL)
    Write-Host ("     {0}" -f $LOG) -ForegroundColor DarkGray
    if ($ageSec -lt 120) {
        Write-Host ("  [OK]   size={0:N1} KB   last update {1}s ago" -f ($lf.Length/1KB), $ageSec) -ForegroundColor Green
    } elseif ($ageSec -lt 1800 -and -not $py) {
        Write-Host ("  [INFO] size={0:N1} KB   last update {1}s ago (engine stopped, log idle - normal)" -f ($lf.Length/1KB), $ageSec) -ForegroundColor Cyan
    } else {
        Write-Host ("  [WARN] size={0:N1} KB   last update {1}s ago (>120s - engine may be stalled)" -f ($lf.Length/1KB), $ageSec) -ForegroundColor Yellow
    }
} else {
    Write-Host "  [INFO] No log file yet for today --  engine has not been started today." -ForegroundColor Cyan
    Write-Host ("         Will appear at: {0}" -f $DAILY_LOG) -ForegroundColor DarkGray
}

# --------------------------------------------------------------------
# 3. trade ledger + slippage summary
# --------------------------------------------------------------------
Write-Host ""
if (Test-Path $TRADES) {
    $count = (Get-Content $TRADES -ErrorAction SilentlyContinue | Measure-Object -Line).Lines
    Write-Host ("  Trade ledger:   {0}" -f $TRADES)
    Write-Host ("  Trades recorded: {0}" -f $count) -ForegroundColor Green

    # Quick slip summary on the last 50 trades (if any)
    if ($count -gt 0) {
        try {
            $tradesObj = Get-Content $TRADES -Tail 50 | ForEach-Object { $_ | ConvertFrom-Json -ErrorAction SilentlyContinue } | Where-Object { $_ -ne $null }
            $slipsAll = $tradesObj | Where-Object { $_.entry_slip_ticks -ne $null } | ForEach-Object { [double]$_.entry_slip_ticks }
            if ($slipsAll.Count -gt 0) {
                $avg = ($slipsAll | Measure-Object -Average).Average
                $max = ($slipsAll | Measure-Object -Maximum).Maximum
                Write-Host ("  Last {0} trades  -- entry slip:  avg={1:N2}t  max={2:N2}t" -f $slipsAll.Count, $avg, $max) -ForegroundColor Cyan
            }
        } catch {}
    }
} else {
    Write-Host "  Trade ledger:   (none yet -- no trades have been executed)"
}

# --------------------------------------------------------------------
# 4. heartbeat line + last 50 lines
# --------------------------------------------------------------------
if ($LOG -and (Test-Path $LOG)) {
    $tail = Get-Content $LOG -Tail 200 -ErrorAction SilentlyContinue
    $hb = $tail | Where-Object { $_ -match 'HEARTBEAT|MARKET STATUS|heartbeat' } | Select-Object -Last 1
    if ($hb) {
        Write-Host ""
        Write-Host "  Latest heartbeat:" -ForegroundColor Cyan
        Write-Host ("     {0}" -f $hb) -ForegroundColor Cyan
    }

    Write-Host ""
    Write-Host "  --- Last 50 log lines -------------------------------------------" -ForegroundColor DarkGray
    Get-Content $LOG -Tail 50 -ErrorAction SilentlyContinue | ForEach-Object {
        if ($_ -match 'ERROR|FAIL|Traceback') {
            Write-Host ("  {0}" -f $_) -ForegroundColor Red
        } elseif ($_ -match 'WARN') {
            Write-Host ("  {0}" -f $_) -ForegroundColor Yellow
        } elseif ($_ -match 'HEARTBEAT|Subscribed|Connected|ALL CHECKS|MARKET STATUS|TRADE_OPEN|TRADE_CLOSE') {
            Write-Host ("  {0}" -f $_) -ForegroundColor Green
        } else {
            Write-Host ("  {0}" -f $_) -ForegroundColor Gray
        }
    }
    Write-Host "  -----------------------------------------------------------------" -ForegroundColor DarkGray
    Write-Host ""
}

# --------------------------------------------------------------------
# 5. tail mode
# --------------------------------------------------------------------
if ($Tail) {
    if (-not $LOG) {
        Write-Host "  -Tail requested but no log to follow. Start the bot first." -ForegroundColor Yellow
        return
    }
    Write-Host ""
    Write-Host ("  Live-tailing {0} (Ctrl-C to exit) ..." -f $LOG) -ForegroundColor Yellow
    Write-Host ""
    Get-Content $LOG -Wait -Tail 0
}
