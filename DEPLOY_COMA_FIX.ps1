# ============================================================================
# DEPLOY COMA FIX — Push patched engine.py to VPS
# ============================================================================
# This deploys:
#   1. Patched engine.py with Coma Detector (OS sleep/freeze protection)
#   2. PREVENT_SLEEP.ps1 (Windows anti-sleep keepalive)
#
# The Coma Detector will:
#   - Detect if the process was frozen > 60 seconds
#   - Emergency close ALL open positions
#   - Force 200-bar re-warm before trading again
#   - Log CRITICAL warnings
#   - Continue running (no crash)
# ============================================================================

$VPS = "20.0.164.200"  # Your VPS IP — change if different
$User = "luke"          # Your VPS username
$SHF = "C:\SHF"        # VPS engine path

Write-Host "=" * 70
Write-Host "  DEPLOYING COMA FIX TO VPS"
Write-Host "=" * 70

# --- Check local files exist ---
if (-not (Test-Path "src\engine.py")) {
    Write-Host "  [ERROR] src\engine.py not found locally!"
    exit 1
}
if (-not (Test-Path "PREVENT_SLEEP.ps1")) {
    Write-Host "  [ERROR] PREVENT_SLEEP.ps1 not found locally!"
    exit 1
}

# --- Verify coma detector is in the engine ---
$content = Get-Content "src\engine.py" -Raw
if ($content -notmatch "COMA DETECTOR") {
    Write-Host "  [ERROR] Coma detector not found in engine.py! Run fix_coma_detector.py first."
    exit 1
}
Write-Host "  [OK] Coma detector verified in local engine.py"

# --- Copy to VPS ---
Write-Host "`n  Copying patched engine.py to VPS..."
try {
    $session = New-PSSession -ComputerName $VPS -Credential (Get-Credential -UserName $User -Message "VPS Password")
    
    # Backup current engine on VPS
    Invoke-Command -Session $session -ScriptBlock {
        Copy-Item "$using:SHF\src\engine.py" "$using:SHF\src\engine.py.bak_pre_coma" -Force
        Write-Host "  [OK] Backed up VPS engine.py"
    }
    
    # Copy patched files
    Copy-Item "src\engine.py" -Destination "$SHF\src\engine.py" -ToSession $session -Force
    Write-Host "  [OK] Deployed patched engine.py"
    
    Copy-Item "PREVENT_SLEEP.ps1" -Destination "$SHF\PREVENT_SLEEP.ps1" -ToSession $session -Force
    Write-Host "  [OK] Deployed PREVENT_SLEEP.ps1"
    
    # Verify
    Invoke-Command -Session $session -ScriptBlock {
        $c = Get-Content "$using:SHF\src\engine.py" -Raw
        if ($c -match "COMA DETECTOR") {
            Write-Host "  [OK] VERIFIED: Coma detector present on VPS"
        } else {
            Write-Host "  [ERROR] Coma detector NOT found on VPS!"
        }
    }
    
    Remove-PSSession $session
    Write-Host "`n  DEPLOYMENT COMPLETE"
} catch {
    Write-Host "  [WARN] PSSession failed. Trying SCP fallback..."
    Write-Host "  Run manually:"
    Write-Host "    scp src\engine.py ${User}@${VPS}:${SHF}\src\engine.py"
    Write-Host "    scp PREVENT_SLEEP.ps1 ${User}@${VPS}:${SHF}\PREVENT_SLEEP.ps1"
}

Write-Host "`n  NEXT STEPS:"
Write-Host "  1. On VPS: Run PREVENT_SLEEP.ps1 in a separate terminal"
Write-Host "  2. On VPS: Restart the bot (RUN_ENGINE.ps1)"
Write-Host "  3. The coma detector is now active — check logs for 'COMA' messages"
Write-Host ""
Write-Host "  If running LOCALLY (not VPS):"
Write-Host "  1. Open a PowerShell terminal and run: .\PREVENT_SLEEP.ps1"
Write-Host "  2. In a SECOND terminal, run: .\RUN_ENGINE.ps1"
Write-Host "  3. Keep BOTH terminals open. Do NOT close the lid on a laptop."
