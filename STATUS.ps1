# ======================================================================
#  STATUS.ps1  -  one-shot health check + live log tail
#
#  Shows:
#    - Whether python (engine) is running
#    - Whether MT5 is running
#    - Last 50 lines of v15_live.log
#    - Number of trades so far
#    - Most recent HEARTBEAT line
#
#  Pass -Tail to stream the log live (Ctrl-C to exit).
# ======================================================================
param(
    [switch]$Tail
)

$ErrorActionPreference = "Continue"
$ROOT   = "C:\PropBot"
$LOG    = Join-Path $ROOT "Results\v15_live.log"
$TRADES = Join-Path $ROOT "Results\v15_live_trades.jsonl"

Write-Host ""
Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host ("  PropBot v15  -  STATUS  {0}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))  -ForegroundColor Cyan
Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host ""

$py  = Get-Process python     -ErrorAction SilentlyContinue
$mt5 = Get-Process terminal64 -ErrorAction SilentlyContinue

if ($py) {
    $pids = ($py | ForEach-Object { $_.Id }) -join ','
    $ram  = [int](($py | Measure-Object WorkingSet -Sum).Sum / 1MB)
    Write-Host ("  [OK]    python engine   RUNNING   pid={0}  ram={1} MB" -f $pids, $ram) -ForegroundColor Green
} else {
    Write-Host "  [FAIL]  python engine   NOT running  - use .\GO_LIVE.ps1 or .\GO_DRYRUN.ps1" -ForegroundColor Red
}

if ($mt5) {
    $mpids = ($mt5 | ForEach-Object { $_.Id }) -join ','
    Write-Host ("  [OK]    MT5 terminal    RUNNING   pid={0}" -f $mpids) -ForegroundColor Green
} else {
    Write-Host "  [FAIL]  MT5 terminal    NOT running" -ForegroundColor Red
}

Write-Host ""
Write-Host ("  Log file: {0}" -f $LOG)
if (Test-Path $LOG) {
    $lf = Get-Item $LOG
    $ageSec = [int]((Get-Date) - $lf.LastWriteTime).TotalSeconds
    if ($ageSec -lt 120) {
        Write-Host ("  [OK]   size={0:N1} KB   last update {1}s ago" -f ($lf.Length/1KB), $ageSec) -ForegroundColor Green
    } else {
        Write-Host ("  [WARN] size={0:N1} KB   last update {1}s ago (>120s - engine may be stalled)" -f ($lf.Length/1KB), $ageSec) -ForegroundColor Yellow
    }
} else {
    Write-Host "  [FAIL] Log file missing - engine has not been started yet." -ForegroundColor Red
}

if (Test-Path $TRADES) {
    $count = (Get-Content $TRADES -ErrorAction SilentlyContinue | Measure-Object -Line).Lines
    Write-Host ""
    Write-Host ("  Trade log: {0}" -f $TRADES)
    Write-Host ("  Trades recorded: {0}" -f $count)
} else {
    Write-Host ""
    Write-Host "  Trade log: (none yet - no trades have been executed)"
}

if (Test-Path $LOG) {
    $hb = Get-Content $LOG -Tail 200 -ErrorAction SilentlyContinue |
          Where-Object { $_ -match "HEARTBEAT" } |
          Select-Object -Last 1
    if ($hb) {
        Write-Host ""
        Write-Host "  Latest heartbeat:" -ForegroundColor Cyan
        Write-Host ("     {0}" -f $hb) -ForegroundColor Cyan
    }

    Write-Host ""
    Write-Host "  --- Last 50 log lines -------------------------------------------" -ForegroundColor DarkGray
    Get-Content $LOG -Tail 50 -ErrorAction SilentlyContinue | ForEach-Object {
        if ($_ -match "ERROR|FAIL") {
            Write-Host ("  {0}" -f $_) -ForegroundColor Red
        } elseif ($_ -match "WARN") {
            Write-Host ("  {0}" -f $_) -ForegroundColor Yellow
        } elseif ($_ -match "HEARTBEAT|Subscribed|Connected|ALL CHECKS") {
            Write-Host ("  {0}" -f $_) -ForegroundColor Green
        } else {
            Write-Host ("  {0}" -f $_) -ForegroundColor Gray
        }
    }
    Write-Host "  -----------------------------------------------------------------" -ForegroundColor DarkGray
    Write-Host ""
}

if ($Tail) {
    Write-Host ""
    Write-Host "  Live-tailing log (Ctrl-C to exit) ..." -ForegroundColor Yellow
    Write-Host ""
    Get-Content $LOG -Wait -Tail 0
}
