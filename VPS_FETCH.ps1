# =============================================================================
# VPS FETCH & DEPLOY v5.6.3 — Paste this into VPS PowerShell
# =============================================================================
# This pulls the latest code from GitHub and copies to C:\SHF
# Run AFTER you've: F4'd the old EA, compiled new EA, F7'd the new EA
# =============================================================================

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "  ============================================="
Write-Host "     SHF v5.6.3 VPS FETCH & DEPLOY"
Write-Host "     Oil + Index Duo"
Write-Host "  ============================================="
Write-Host ""

# ---- Step 1: Kill any running engine ----
Write-Host "[1/5] Stopping any running engine..."
$pyProcs = Get-Process python -ErrorAction SilentlyContinue
if ($pyProcs) {
    $pyProcs | Stop-Process -Force
    Write-Host "  Killed $($pyProcs.Count) Python process(es)"
    Start-Sleep 2
} else {
    Write-Host "  No running engine found"
}

# ---- Step 2: Git pull ----
Write-Host "[2/5] Pulling latest from GitHub..."
Set-Location "C:\SHF"
git pull origin main 2>&1
Write-Host "  Done"

# ---- Step 3: Verify key files ----
Write-Host "[3/5] Verifying files..."
$files = @("shf_core.pyd","src\engine.py","src\execution\mt5_bridge.py","src\risk\akad_risk.py","src\strategies\hmm_regime.py","RUN_ENGINE.ps1")
$ok = $true
foreach ($f in $files) {
    if (Test-Path $f) {
        Write-Host "  [OK] $f"
    } else {
        Write-Host "  [MISSING] $f"
        $ok = $false
    }
}

# ---- Step 4: Quick Python import check ----
Write-Host "[4/5] Testing Python imports..."
python -c "from shf_core import CointegrationEngine; from src.engine import HOLY_TRIO; print(f'  Pairs: {[p.name for p in HOLY_TRIO]}'); print('  [OK] All imports work')" 2>&1

# ---- Step 5: Ready ----
Write-Host ""
Write-Host "[5/5] READY TO LAUNCH"
Write-Host ""
Write-Host "  Next steps:"
Write-Host "    1. Make sure MT5 is open"
Write-Host "    2. F4 old EA -> compile new SHF_Bridge.mq5 (v5.63) -> F7 attach to chart"
Write-Host "    3. Run:  powershell -ExecutionPolicy Bypass -File C:\SHF\RUN_ENGINE.ps1"
Write-Host ""
