# ╔══════════════════════════════════════════════════════════════════════╗
# ║  STATUS.ps1  —  one-shot health check + live log tail                ║
# ║                                                                        ║
# ║  Shows:                                                                ║
# ║    • Whether python (engine) is running                               ║
# ║    • Whether MT5 is running                                            ║
# ║    • Last 50 lines of v15_live.log                                    ║
# ║    • Number of trades so far                                          ║
# ║    • Most recent HEARTBEAT line                                       ║
# ║                                                                        ║
# ║  Pass -Tail to stream the log live (Ctrl-C to exit).                  ║
# ╚══════════════════════════════════════════════════════════════════════╝
param(
    [switch]$Tail
)

$ErrorActionPreference = "Continue"
$ROOT     = "C:\PropBot"
$LOG      = Join-Path $ROOT "Results\v15_live.log"
$TRADES   = Join-Path $ROOT "Results\v15_live_trades.jsonl"

Write-Host "`n═══════════════════════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  PropBot v15  —  STATUS  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"  -ForegroundColor Cyan
Write-Host "═══════════════════════════════════════════════════════════════════════`n" -ForegroundColor Cyan

# 1. Process state
$py  = Get-Process python     -ErrorAction SilentlyContinue
$mt5 = Get-Process terminal64 -ErrorAction SilentlyContinue

if ($py) {
    Write-Host "  ✅ python engine   RUNNING   pid=$($py.Id -join ',') cpu%=$([int]($py.CPU))  ram=$([int]($py.WorkingSet/1MB)) MB" -ForegroundColor Green
} else {
    Write-Host "  ❌ python engine   NOT running — use .\GO_LIVE.ps1 or .\GO_DRYRUN.ps1" -ForegroundColor Red
}

if ($mt5) {
    Write-Host "  ✅ MT5 terminal    RUNNING   pid=$($mt5.Id -join ',')" -ForegroundColor Green
} else {
    Write-Host "  ❌ MT5 terminal    NOT running" -ForegroundColor Red
}

# 2. Log file existence + size
Write-Host "`n  Log file: $LOG"
if (Test-Path $LOG) {
    $lf = Get-Item $LOG
    $ageSec = (Get-Date) - $lf.LastWriteTime | Select-Object -ExpandProperty TotalSeconds
    $fresh = $ageSec -lt 120
    $color = if ($fresh) { "Green" } else { "Yellow" }
    Write-Host ("  {0} size={1:N1} KB   last update {2:N0}s ago" -f `
               $(if ($fresh){"✅"}else{"⚠️ "}), ($lf.Length/1KB), $ageSec) -ForegroundColor $color
} else {
    Write-Host "  ❌ Log file missing — engine hasn't been started yet." -ForegroundColor Red
}

# 3. Trade count
if (Test-Path $TRADES) {
    $count = (Get-Content $TRADES -ErrorAction SilentlyContinue | Measure-Object -Line).Lines
    Write-Host "`n  Trade log: $TRADES"
    Write-Host "  📊 Trades recorded: $count"
} else {
    Write-Host "`n  Trade log: (none yet — no trades have been executed)"
}

# 4. Most recent HEARTBEAT
if (Test-Path $LOG) {
    $hb = Get-Content $LOG -Tail 200 -ErrorAction SilentlyContinue |
          Where-Object { $_ -match "HEARTBEAT" } |
          Select-Object -Last 1
    if ($hb) {
        Write-Host "`n  Latest heartbeat:`n     $hb" -ForegroundColor Cyan
    }
}

# 5. Last 50 log lines
if (Test-Path $LOG) {
    Write-Host "`n  ─── Last 50 log lines ─────────────────────────────────────────────" -ForegroundColor DarkGray
    Get-Content $LOG -Tail 50 -ErrorAction SilentlyContinue | ForEach-Object {
        if ($_ -match "ERROR|FAIL") {
            Write-Host "  $_" -ForegroundColor Red
        } elseif ($_ -match "WARN") {
            Write-Host "  $_" -ForegroundColor Yellow
        } elseif ($_ -match "HEARTBEAT|Subscribed|✅|Connected") {
            Write-Host "  $_" -ForegroundColor Green
        } else {
            Write-Host "  $_" -ForegroundColor Gray
        }
    }
    Write-Host "  ───────────────────────────────────────────────────────────────────`n" -ForegroundColor DarkGray
}

# 6. Optional live tail
if ($Tail) {
    Write-Host "`n  Live-tailing log (Ctrl-C to exit) …`n" -ForegroundColor Yellow
    Get-Content $LOG -Wait -Tail 0
}
