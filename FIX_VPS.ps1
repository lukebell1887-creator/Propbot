# ============================================================================
# FIX VPS - Re-copy all files properly (fixes null byte corruption)
# ============================================================================
# PASTE THIS INTO POWERSHELL ON THE VPS
# ============================================================================

$ErrorActionPreference = "Continue"
$SOURCE = "\\tsclient\C\Users\lukeb\OneDrive\Desktop\Betting"

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  FIX VPS - Re-copying files properly" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan

# Check if tsclient is accessible
if (-Not (Test-Path $SOURCE)) {
    Write-Host "ERROR: Cannot access $SOURCE" -ForegroundColor Red
    Write-Host "Make sure you enabled Drive Sharing in RDP settings!" -ForegroundColor Red
    Write-Host "RDP > Show Options > Local Resources > More > tick Drives" -ForegroundColor Yellow
    exit 1
}

Write-Host "`n[1/4] Removing old src folder..." -ForegroundColor Yellow
if (Test-Path "C:\SHF\src") {
    Remove-Item -Recurse -Force "C:\SHF\src"
}
Write-Host "  Done" -ForegroundColor Green

Write-Host "[2/4] Copying full src folder..." -ForegroundColor Yellow
Copy-Item -Recurse -Force "$SOURCE\src" "C:\SHF\src"
Write-Host "  Done" -ForegroundColor Green

Write-Host "[3/4] Copying shf_core.pyd..." -ForegroundColor Yellow
Copy-Item -Force "$SOURCE\shf_core.pyd" "C:\SHF\shf_core.pyd"
Write-Host "  Done" -ForegroundColor Green

Write-Host "[4/4] Copying docs folder..." -ForegroundColor Yellow
if (-Not (Test-Path "C:\SHF\docs")) {
    New-Item -ItemType Directory -Path "C:\SHF\docs" -Force | Out-Null
}
Copy-Item -Force "$SOURCE\docs\*" "C:\SHF\docs\"
Write-Host "  Done" -ForegroundColor Green

# Verify no null bytes in key files
Write-Host "`n============================================" -ForegroundColor Cyan
Write-Host "  VERIFICATION" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan

$filesToCheck = @(
    "C:\SHF\src\execution\__init__.py",
    "C:\SHF\src\risk\__init__.py",
    "C:\SHF\src\strategies\__init__.py",
    "C:\SHF\src\engine.py",
    "C:\SHF\src\risk\akad_risk.py",
    "C:\SHF\src\execution\mt5_bridge.py"
)

$allGood = $true
foreach ($file in $filesToCheck) {
    if (Test-Path $file) {
        $bytes = [System.IO.File]::ReadAllBytes($file)
        $hasNull = $false
        foreach ($b in $bytes) {
            if ($b -eq 0) { $hasNull = $true; break }
        }
        if ($hasNull) {
            Write-Host "  CORRUPTED: $file (has null bytes)" -ForegroundColor Red
            $allGood = $false
        } else {
            Write-Host "  OK: $file" -ForegroundColor Green
        }
    } else {
        Write-Host "  MISSING: $file" -ForegroundColor Red
        $allGood = $false
    }
}

Write-Host "`n============================================" -ForegroundColor Cyan
if ($allGood) {
    Write-Host "  ALL FILES OK! Now run:" -ForegroundColor Green
    Write-Host "  cd C:\SHF" -ForegroundColor White
    Write-Host "  python -m src.engine --dry-run" -ForegroundColor White
} else {
    Write-Host "  SOME FILES CORRUPTED - see above" -ForegroundColor Red
    Write-Host "  Try: manually re-type the __init__.py files" -ForegroundColor Yellow
}
Write-Host "============================================" -ForegroundColor Cyan
