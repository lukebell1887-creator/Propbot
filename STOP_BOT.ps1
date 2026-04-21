# ╔══════════════════════════════════════════════════════════════════════╗
# ║  STOP_BOT.ps1  —  graceful kill for Python + optional MT5            ║
# ║                                                                        ║
# ║  1. Sends Ctrl-C to any python running run_v15_live.py → engine       ║
# ║     shuts down its threads + writes FINAL SESSION SUMMARY             ║
# ║  2. If that's ignored, force-kills python                             ║
# ║  3. Does NOT close MT5 by default (open positions keep broker-held    ║
# ║     SL/TP intact). Pass -AlsoMT5 to close MT5 too.                    ║
# ║                                                                        ║
# ║  Usage:                                                                ║
# ║     .\STOP_BOT.ps1                (stop python only)                  ║
# ║     .\STOP_BOT.ps1 -AlsoMT5       (stop python AND MT5)               ║
# ╚══════════════════════════════════════════════════════════════════════╝
param(
    [switch]$AlsoMT5
)

$ErrorActionPreference = "Continue"

Write-Host "`n═══════════════════════════════════════════════════════════════════════" -ForegroundColor Red
Write-Host "  STOPPING v15 bot"  -ForegroundColor Red
Write-Host "═══════════════════════════════════════════════════════════════════════`n" -ForegroundColor Red

# 1. Try graceful Ctrl-C first
$pyProcs = Get-Process python -ErrorAction SilentlyContinue
if ($pyProcs) {
    Write-Host "[1/3] Sending Ctrl-C to $($pyProcs.Count) python process(es) for clean shutdown …" -ForegroundColor Cyan
    foreach ($p in $pyProcs) {
        try {
            # Send WM_CLOSE — python catches KeyboardInterrupt-style shutdown
            $p.CloseMainWindow() | Out-Null
        } catch {}
    }
    Start-Sleep -Seconds 5
}

# 2. Force-kill anything still running
$still = Get-Process python -ErrorAction SilentlyContinue
if ($still) {
    Write-Host "[2/3] Force-killing remaining python process(es) …" -ForegroundColor Yellow
    $still | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}
else {
    Write-Host "[2/3] All python processes stopped cleanly." -ForegroundColor Green
}

# 3. Optional MT5 kill
if ($AlsoMT5) {
    Write-Host "[3/3] -AlsoMT5 passed → stopping MT5 …" -ForegroundColor Cyan
    Get-Process terminal64 -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
}
else {
    Write-Host "[3/3] MT5 left running (open positions keep their broker-held SL/TP)." -ForegroundColor Green
    Write-Host "       Use  .\STOP_BOT.ps1 -AlsoMT5  if you want to close MT5 too." -ForegroundColor DarkGray
}

Write-Host "`n✅  Bot stopped.`n" -ForegroundColor Green
