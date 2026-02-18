# ============================================================================
# UPDATE VPS — Deploy Native TCP Socket Bridge (replaces ZMQ)
# ============================================================================
# Paste this into PowerShell on the VPS
# ============================================================================

$ErrorActionPreference = "Continue"
$SRC = "\\tsclient\C\Users\lukeb\OneDrive\Desktop\PropBot"
$DST = "C:\SHF"

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Updating SHF v5.7 — Gold/Silver" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan

# Step 1: Update Python bridge
Write-Host "`n[1/4] Updating Python bridge..." -ForegroundColor Yellow
Copy-Item -Force "$SRC\src\execution\mt5_bridge.py" "$DST\src\execution\mt5_bridge.py"
Write-Host "  mt5_bridge.py updated (TCP server mode)" -ForegroundColor Green

# Step 2: Update requirements
Write-Host "[2/4] Updating requirements..." -ForegroundColor Yellow
Copy-Item -Force "$SRC\requirements.txt" "$DST\requirements.txt"
Write-Host "  requirements.txt updated (pyzmq removed)" -ForegroundColor Green

# Step 3: Copy new EA to MT5 terminals
Write-Host "[3/4] Deploying SHF_Bridge.mq5 to MT5..." -ForegroundColor Yellow
$mt5Base = "$env:APPDATA\MetaQuotes\Terminal"
$eaCopied = 0
if (Test-Path $mt5Base) {
    Get-ChildItem -Path $mt5Base -Directory | ForEach-Object {
        $expertsDir = "$($_.FullName)\MQL5\Experts"
        if (Test-Path $expertsDir) {
            Copy-Item -Force "$SRC\MQL5\Experts\SHF_Bridge.mq5" "$expertsDir\SHF_Bridge.mq5"
            Write-Host "  Copied to: $expertsDir" -ForegroundColor Green
            $eaCopied++
        }
    }
}
Write-Host "  EA deployed to $eaCopied terminal(s)" -ForegroundColor Green

# Step 4: Add localhost to MT5 allowed connections
Write-Host "[4/4] Checking MT5 socket permissions..." -ForegroundColor Yellow
Write-Host "  IMPORTANT: In MT5, go to:" -ForegroundColor White
Write-Host "    Tools -> Options -> Expert Advisors" -ForegroundColor White
Write-Host "    Enable: 'Allow WebRequest for listed URL'" -ForegroundColor White
Write-Host "    The EA uses SocketConnect to localhost:5555" -ForegroundColor White
Write-Host "    Make sure 'Allow DLL imports' is checked when attaching EA" -ForegroundColor White

Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host "  UPDATE COMPLETE" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host ""
Write-Host "  NEXT STEPS:" -ForegroundColor White
Write-Host "  1. Open MetaEditor (F4 in MT5)" -ForegroundColor White
Write-Host "  2. File -> Open -> SHF_Bridge.mq5" -ForegroundColor White
Write-Host "     (in Experts folder)" -ForegroundColor White
Write-Host "  3. Press F7 to compile" -ForegroundColor White
Write-Host "  4. Expect: 0 errors (no external libraries!)" -ForegroundColor White
Write-Host "  5. Go to MT5, drag SHF_Bridge onto a chart" -ForegroundColor White
Write-Host "     (tick Allow DLL imports)" -ForegroundColor White
Write-Host "  6. Start Python: cd C:\SHF && python -m src.engine" -ForegroundColor White
Write-Host "  7. EA auto-connects to Python server on port 5555" -ForegroundColor White
Write-Host ""
