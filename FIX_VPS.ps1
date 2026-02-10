# ============================================================================
# FIX VPS — Quick re-sync all v5.6 files (no nuke, just overwrite)
# ============================================================================
# Use this after making changes locally and wanting to push to VPS.
# For a FULL fresh install, use DEPLOY_VPS_FRESH.ps1 instead.
#
# PASTE THIS INTO POWERSHELL ON THE VPS
# ============================================================================

$ErrorActionPreference = "Continue"
$SOURCE = "\\tsclient\C\Users\lukeb\OneDrive\Desktop\PropBot"
$TARGET = "C:\SHF"

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  SHF v5.6 — Quick Re-Sync to VPS" -ForegroundColor Cyan
Write-Host "  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan

# Check if tsclient is accessible
if (-Not (Test-Path $SOURCE)) {
    Write-Host "ERROR: Cannot access $SOURCE" -ForegroundColor Red
    Write-Host "Make sure you enabled Drive Sharing in RDP settings!" -ForegroundColor Red
    Write-Host "RDP > Show Options > Local Resources > More > tick Drives" -ForegroundColor Yellow
    exit 1
}

# Kill running Python to release file locks
Write-Host "`n[1/5] Stopping any running Python..." -ForegroundColor Yellow
Get-Process python* -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
Write-Host "  Done" -ForegroundColor Green

# Re-copy src folder
Write-Host "[2/5] Syncing src/ folder..." -ForegroundColor Yellow
if (Test-Path "$TARGET\src") {
    Remove-Item -Recurse -Force "$TARGET\src"
}
New-Item -ItemType Directory -Path "$TARGET\src" -Force | Out-Null
New-Item -ItemType Directory -Path "$TARGET\src\execution" -Force | Out-Null
New-Item -ItemType Directory -Path "$TARGET\src\risk" -Force | Out-Null
New-Item -ItemType Directory -Path "$TARGET\src\strategies" -Force | Out-Null

Copy-Item -Force "$SOURCE\src\__init__.py" "$TARGET\src\__init__.py"
Copy-Item -Force "$SOURCE\src\engine.py" "$TARGET\src\engine.py"
Copy-Item -Force "$SOURCE\src\execution\__init__.py" "$TARGET\src\execution\__init__.py"
Copy-Item -Force "$SOURCE\src\execution\mt5_bridge.py" "$TARGET\src\execution\mt5_bridge.py"
Copy-Item -Force "$SOURCE\src\risk\__init__.py" "$TARGET\src\risk\__init__.py"
Copy-Item -Force "$SOURCE\src\risk\akad_risk.py" "$TARGET\src\risk\akad_risk.py"
Copy-Item -Force "$SOURCE\src\risk\supervisor.py" "$TARGET\src\risk\supervisor.py"
Copy-Item -Force "$SOURCE\src\strategies\__init__.py" "$TARGET\src\strategies\__init__.py"
Copy-Item -Force "$SOURCE\src\strategies\hmm_regime.py" "$TARGET\src\strategies\hmm_regime.py"
Write-Host "  Done" -ForegroundColor Green

# Re-copy Rust binary
Write-Host "[3/5] Syncing shf_core.pyd..." -ForegroundColor Yellow
Copy-Item -Force "$SOURCE\shf_core.pyd" "$TARGET\shf_core.pyd"
Write-Host "  Done" -ForegroundColor Green

# Re-copy EA
Write-Host "[4/5] Syncing MQL5 EA..." -ForegroundColor Yellow
if (-Not (Test-Path "$TARGET\MQL5\Experts")) {
    New-Item -ItemType Directory -Path "$TARGET\MQL5\Experts" -Force | Out-Null
}
Copy-Item -Force "$SOURCE\MQL5\Experts\SHF_ZMQ_Bridge.mq5" "$TARGET\MQL5\Experts\SHF_ZMQ_Bridge.mq5"

# Also copy to MT5 data folder if found
$mt5Base = "$env:APPDATA\MetaQuotes\Terminal"
if (Test-Path $mt5Base) {
    Get-ChildItem -Path $mt5Base -Directory | ForEach-Object {
        $expertDir = "$($_.FullName)\MQL5\Experts"
        if (Test-Path $expertDir) {
            Copy-Item -Force "$SOURCE\MQL5\Experts\SHF_ZMQ_Bridge.mq5" "$expertDir\SHF_ZMQ_Bridge.mq5"
            Write-Host "  Also copied EA to: $expertDir" -ForegroundColor Gray
        }
    }
}
Write-Host "  Done" -ForegroundColor Green

# Verify critical files
Write-Host "[5/5] Verifying..." -ForegroundColor Yellow
$filesToCheck = @(
    "$TARGET\shf_core.pyd",
    "$TARGET\src\engine.py",
    "$TARGET\src\execution\mt5_bridge.py",
    "$TARGET\src\risk\akad_risk.py",
    "$TARGET\src\risk\supervisor.py",
    "$TARGET\src\strategies\hmm_regime.py"
)

$allGood = $true
foreach ($file in $filesToCheck) {
    if (Test-Path $file) {
        $size = (Get-Item $file).Length
        if ($size -eq 0) {
            Write-Host "  EMPTY: $file" -ForegroundColor Red
            $allGood = $false
        } elseif ($file -notlike "*.pyd") {
            $bytes = [System.IO.File]::ReadAllBytes($file)
            $hasNull = $false
            $checkLimit = [Math]::Min($bytes.Length, 500)
            for ($i = 0; $i -lt $checkLimit; $i++) {
                if ($bytes[$i] -eq 0) { $hasNull = $true; break }
            }
            if ($hasNull) {
                Write-Host "  CORRUPTED: $file" -ForegroundColor Red
                $allGood = $false
            } else {
                Write-Host "  OK: $(Split-Path $file -Leaf) ($([Math]::Round($size/1024,1))KB)" -ForegroundColor Green
            }
        } else {
            Write-Host "  OK: $(Split-Path $file -Leaf) ($([Math]::Round($size/1024,1))KB)" -ForegroundColor Green
        }
    } else {
        Write-Host "  MISSING: $file" -ForegroundColor Red
        $allGood = $false
    }
}

Write-Host "`n============================================" -ForegroundColor Cyan
if ($allGood) {
    Write-Host "  ALL FILES SYNCED OK!" -ForegroundColor Green
    Write-Host ""
    Write-Host "  To start the engine:" -ForegroundColor White
    Write-Host "    cd $TARGET" -ForegroundColor Cyan
    Write-Host "    python -m src.engine" -ForegroundColor Cyan
} else {
    Write-Host "  SOME FILES HAVE ISSUES — see above" -ForegroundColor Red
}
Write-Host "============================================" -ForegroundColor Cyan
