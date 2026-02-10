# ============================================
# ONE-CLICK FIX: Copy new EA + run test
# ============================================
# Paste this into VPS PowerShell
# ============================================

$SRC = "\\tsclient\C\Users\lukeb\OneDrive\Desktop\PropBot"

Write-Host ""
Write-Host "Step 1: Copying new EA to ALL MT5 folders..." -ForegroundColor Yellow
$mt5Base = "$env:APPDATA\MetaQuotes\Terminal"
if (Test-Path $mt5Base) {
    Get-ChildItem -Path $mt5Base -Directory | ForEach-Object {
        $expertsDir = "$($_.FullName)\MQL5\Experts"
        if (Test-Path $expertsDir) {
            Copy-Item -Force "$SRC\MQL5\Experts\SHF_Bridge.mq5" "$expertsDir\SHF_Bridge.mq5"
            Write-Host "  Copied to: $expertsDir" -ForegroundColor Green
        }
    }
}

Write-Host ""
Write-Host "Step 2: Copying test script..." -ForegroundColor Yellow
Copy-Item -Force "$SRC\Scripts\test_tcp_bridge.py" "C:\SHF\Scripts\test_tcp_bridge.py"
Write-Host "  Done" -ForegroundColor Green

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  NOW DO THESE 3 THINGS IN MT5:" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  1. Click the SMILEY FACE on your chart" -ForegroundColor White
Write-Host "     (or right-click chart -> Expert Advisors -> Remove)" -ForegroundColor Gray
Write-Host ""
Write-Host "  2. Press F4 to open MetaEditor" -ForegroundColor White
Write-Host "     Find SHF_Bridge.mq5 in the left panel" -ForegroundColor Gray
Write-Host "     Double-click it, then press F7 (compile)" -ForegroundColor Gray
Write-Host "     Wait for '0 errors' at the bottom" -ForegroundColor Gray
Write-Host "     Close MetaEditor (Alt+F4)" -ForegroundColor Gray
Write-Host ""
Write-Host "  3. In MT5 Navigator (Ctrl+N), under Expert Advisors" -ForegroundColor White
Write-Host "     Drag 'SHF_Bridge' onto your chart" -ForegroundColor Gray
Write-Host "     Tick 'Allow DLL imports' -> click OK" -ForegroundColor Gray
Write-Host ""
Write-Host "  4. Look at the EXPERTS tab at the bottom of MT5" -ForegroundColor White
Write-Host "     You should see 'v5.61' and 'Found: AUDUSD'" -ForegroundColor Gray
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  After doing those 3 things, press ENTER" -ForegroundColor Cyan
Write-Host "  to run the connection test." -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

Read-Host "Press ENTER when ready"

Write-Host ""
Write-Host "Running test..." -ForegroundColor Yellow
Set-Location C:\SHF
python Scripts\test_tcp_bridge.py
