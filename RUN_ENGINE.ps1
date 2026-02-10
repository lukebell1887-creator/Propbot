# ============================================
# Copy latest Python files + run engine
# Paste into VPS PowerShell
# ============================================

$SRC = "\\tsclient\C\Users\lukeb\OneDrive\Desktop\PropBot"

Write-Host "Copying Python files to VPS..." -ForegroundColor Yellow
Copy-Item -Force "$SRC\src\engine.py" "C:\SHF\src\engine.py"
Copy-Item -Force "$SRC\src\execution\mt5_bridge.py" "C:\SHF\src\execution\mt5_bridge.py"
Copy-Item -Force "$SRC\src\risk\akad_risk.py" "C:\SHF\src\risk\akad_risk.py"
Write-Host "  Done" -ForegroundColor Green

Write-Host ""
Write-Host "Starting engine..." -ForegroundColor Yellow
Set-Location C:\SHF
python -m src.engine
