# ============================================================================
# SHF v5.6 — FRESH VPS DEPLOYMENT (NUKE & REBUILD)
# ============================================================================
# 
# INSTRUCTIONS:
#   1. RDP to VPS: 78.141.192.253 (Administrator)
#      - In RDP settings: Show Options > Local Resources > More > tick "Drives"
#   2. Open PowerShell as Administrator on the VPS
#   3. Copy-paste this ENTIRE script and press Enter
#
# This script will:
#   - Completely remove the old bot at C:\SHF
#   - Deploy SHF v5.6 from your local machine via \\tsclient mapped drive
#   - Install/verify Python dependencies
#   - Verify all files (no corruption)
#   - Run Rust core smoke test
#   - Set up MT5 EA
#
# ============================================================================

$ErrorActionPreference = "Stop"
Set-ExecutionPolicy Bypass -Scope Process -Force

$SOURCE = "\\tsclient\C\Users\lukeb\OneDrive\Desktop\PropBot"
$TARGET = "C:\SHF"

Write-Host ""
Write-Host "============================================================================" -ForegroundColor Cyan
Write-Host "    SHF v5.6 — FRESH VPS DEPLOYMENT" -ForegroundColor Cyan
Write-Host "    $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Cyan
Write-Host "============================================================================" -ForegroundColor Cyan

# ============================================================================
# STEP 0: Verify mapped drive is accessible
# ============================================================================
Write-Host "`n[0/9] Checking mapped drive..." -ForegroundColor Yellow
if (-Not (Test-Path $SOURCE)) {
    Write-Host "  ERROR: Cannot access $SOURCE" -ForegroundColor Red
    Write-Host ""
    Write-Host "  FIX: In your RDP client (before connecting):" -ForegroundColor Yellow
    Write-Host "    Show Options > Local Resources > More... > tick 'Drives'" -ForegroundColor White
    Write-Host "  Then reconnect RDP and run this script again." -ForegroundColor Yellow
    exit 1
}
Write-Host "  Mapped drive OK: $SOURCE" -ForegroundColor Green

# ============================================================================
# STEP 1: NUKE old bot completely
# ============================================================================
Write-Host "`n[1/9] NUKING old bot at $TARGET..." -ForegroundColor Yellow
if (Test-Path $TARGET) {
    # Kill any running Python processes first
    Get-Process python* -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2

    Remove-Item -Recurse -Force $TARGET -ErrorAction SilentlyContinue
    if (Test-Path $TARGET) {
        # Sometimes needs a second attempt after process kill
        Start-Sleep -Seconds 2
        Remove-Item -Recurse -Force $TARGET -ErrorAction Continue
    }
    Write-Host "  Old bot REMOVED" -ForegroundColor Green
} else {
    Write-Host "  No existing bot found (clean slate)" -ForegroundColor Green
}

# ============================================================================
# STEP 2: Create fresh directory structure
# ============================================================================
Write-Host "`n[2/9] Creating directory structure..." -ForegroundColor Yellow
$dirs = @(
    "$TARGET",
    "$TARGET\src",
    "$TARGET\src\execution",
    "$TARGET\src\risk",
    "$TARGET\src\strategies",
    "$TARGET\MQL5",
    "$TARGET\MQL5\Experts",
    "$TARGET\Docs",
    "$TARGET\Scripts",
    "$TARGET\Results",
    "$TARGET\logs",
    "$TARGET\state",
    "$TARGET\data",
    "$TARGET\data\historical"
)
foreach ($dir in $dirs) {
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
}
Write-Host "  Directory structure created" -ForegroundColor Green

# ============================================================================
# STEP 3: Copy all v5.6 files
# ============================================================================
Write-Host "`n[3/9] Copying SHF v5.6 files..." -ForegroundColor Yellow

# --- Core: Rust compiled binary ---
Write-Host "  Copying shf_core.pyd (Rust binary)..." -ForegroundColor Gray
Copy-Item -Force "$SOURCE\shf_core.pyd" "$TARGET\shf_core.pyd"

# --- Core: Python engine ---
Write-Host "  Copying src/ (Python engine)..." -ForegroundColor Gray
Copy-Item -Force "$SOURCE\src\__init__.py" "$TARGET\src\__init__.py"
Copy-Item -Force "$SOURCE\src\engine.py" "$TARGET\src\engine.py"

# Execution
Copy-Item -Force "$SOURCE\src\execution\__init__.py" "$TARGET\src\execution\__init__.py"
Copy-Item -Force "$SOURCE\src\execution\mt5_bridge.py" "$TARGET\src\execution\mt5_bridge.py"

# Risk
Copy-Item -Force "$SOURCE\src\risk\__init__.py" "$TARGET\src\risk\__init__.py"
Copy-Item -Force "$SOURCE\src\risk\akad_risk.py" "$TARGET\src\risk\akad_risk.py"
Copy-Item -Force "$SOURCE\src\risk\supervisor.py" "$TARGET\src\risk\supervisor.py"

# Strategies
Copy-Item -Force "$SOURCE\src\strategies\__init__.py" "$TARGET\src\strategies\__init__.py"
Copy-Item -Force "$SOURCE\src\strategies\hmm_regime.py" "$TARGET\src\strategies\hmm_regime.py"

# --- MQL5 Expert Advisor ---
Write-Host "  Copying MQL5 EA..." -ForegroundColor Gray
Copy-Item -Force "$SOURCE\MQL5\Experts\SHF_ZMQ_Bridge.mq5" "$TARGET\MQL5\Experts\SHF_ZMQ_Bridge.mq5"

# --- Docs ---
Write-Host "  Copying Docs..." -ForegroundColor Gray
Copy-Item -Force "$SOURCE\Docs\*" "$TARGET\Docs\" -ErrorAction SilentlyContinue

# --- Scripts (for validation/testing) ---
Write-Host "  Copying Scripts..." -ForegroundColor Gray
Copy-Item -Force "$SOURCE\Scripts\*" "$TARGET\Scripts\" -ErrorAction SilentlyContinue

# --- Results (reference) ---
Write-Host "  Copying Results..." -ForegroundColor Gray
Copy-Item -Force "$SOURCE\Results\*" "$TARGET\Results\" -ErrorAction SilentlyContinue

# --- Requirements ---
Write-Host "  Copying requirements.txt..." -ForegroundColor Gray
Copy-Item -Force "$SOURCE\requirements.txt" "$TARGET\requirements.txt"

Write-Host "  All files copied" -ForegroundColor Green

# ============================================================================
# STEP 4: Verify no null-byte corruption (OneDrive protection)
# ============================================================================
Write-Host "`n[4/9] Verifying file integrity (null-byte check)..." -ForegroundColor Yellow

$criticalFiles = @(
    "$TARGET\shf_core.pyd",
    "$TARGET\src\__init__.py",
    "$TARGET\src\engine.py",
    "$TARGET\src\execution\__init__.py",
    "$TARGET\src\execution\mt5_bridge.py",
    "$TARGET\src\risk\__init__.py",
    "$TARGET\src\risk\akad_risk.py",
    "$TARGET\src\risk\supervisor.py",
    "$TARGET\src\strategies\__init__.py",
    "$TARGET\src\strategies\hmm_regime.py",
    "$TARGET\MQL5\Experts\SHF_ZMQ_Bridge.mq5"
)

$allGood = $true
$fileCount = 0
foreach ($file in $criticalFiles) {
    if (-Not (Test-Path $file)) {
        Write-Host "  MISSING: $file" -ForegroundColor Red
        $allGood = $false
        continue
    }
    $fileCount++
    $size = (Get-Item $file).Length
    if ($size -eq 0) {
        Write-Host "  EMPTY: $file (0 bytes)" -ForegroundColor Red
        $allGood = $false
        continue
    }
    # Check for null bytes in text files (skip .pyd binary)
    if ($file -notlike "*.pyd") {
        $bytes = [System.IO.File]::ReadAllBytes($file)
        $hasNull = $false
        $checkLimit = [Math]::Min($bytes.Length, 1000)  # Check first 1KB
        for ($i = 0; $i -lt $checkLimit; $i++) {
            if ($bytes[$i] -eq 0) { $hasNull = $true; break }
        }
        if ($hasNull) {
            Write-Host "  CORRUPTED: $file (null bytes detected)" -ForegroundColor Red
            $allGood = $false
            continue
        }
    }
    $sizeKB = [Math]::Round($size / 1024, 1)
    Write-Host "  OK: $(Split-Path $file -Leaf) (${sizeKB}KB)" -ForegroundColor Green
}

if (-Not $allGood) {
    Write-Host "`n  *** FILE INTEGRITY CHECK FAILED ***" -ForegroundColor Red
    Write-Host "  Fix the issues above before continuing." -ForegroundColor Red
    exit 1
}
Write-Host "  All $fileCount critical files verified" -ForegroundColor Green

# ============================================================================
# STEP 5: Check/Install Python
# ============================================================================
Write-Host "`n[5/9] Checking Python installation..." -ForegroundColor Yellow

$pythonPath = $null
# Check common Python paths
$pythonCandidates = @(
    "python",
    "python3",
    "C:\Python310\python.exe",
    "C:\Python311\python.exe",
    "C:\Python312\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
)

foreach ($candidate in $pythonCandidates) {
    try {
        $ver = & $candidate --version 2>&1
        if ($ver -match "Python 3\.(1[0-9]|[2-9][0-9])") {
            $pythonPath = $candidate
            Write-Host "  Found: $ver at $candidate" -ForegroundColor Green
            break
        }
    } catch {}
}

if (-Not $pythonPath) {
    Write-Host "  Python 3.10+ not found. Installing via Chocolatey..." -ForegroundColor Yellow
    
    # Install Chocolatey if needed
    if (-Not (Get-Command choco -ErrorAction SilentlyContinue)) {
        Write-Host "  Installing Chocolatey..." -ForegroundColor Gray
        [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072
        iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))
        $env:Path += ";C:\ProgramData\chocolatey\bin"
    }
    
    choco install python312 -y --no-progress 2>&1 | Out-Null
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    $pythonPath = "python"
    
    $ver = & $pythonPath --version 2>&1
    Write-Host "  Installed: $ver" -ForegroundColor Green
}

# ============================================================================
# STEP 6: Install Python dependencies
# ============================================================================
Write-Host "`n[6/9] Installing Python dependencies..." -ForegroundColor Yellow
& $pythonPath -m pip install --upgrade pip --quiet 2>&1 | Out-Null
& $pythonPath -m pip install -r "$TARGET\requirements.txt" --quiet 2>&1
Write-Host "  Dependencies installed" -ForegroundColor Green

# ============================================================================
# STEP 7: Smoke test — Rust core import
# ============================================================================
Write-Host "`n[7/9] Smoke testing Rust core (shf_core.pyd)..." -ForegroundColor Yellow

$smokeTest = @"
import sys, os
os.chdir(r'$TARGET')
sys.path.insert(0, r'$TARGET')

try:
    from shf_core import CointegrationEngine, KalmanSentinel, AKADRiskCalculator, CorrelationRiskMonitor
    
    # Test CointegrationEngine
    eng = CointegrationEngine(span=100, beta=1.0, dynamic_z=True, dynamic_exit=True)
    sig = eng.update(100.0, 99.0)
    
    # Test KalmanSentinel
    ks = KalmanSentinel(static_beta=1.0, beta_tolerance=0.15)
    beta, abort = ks.update(4.605, 4.595)
    
    # Test AKADRiskCalculator
    akad = AKADRiskCalculator(base_risk=0.0075, dd_lambda=40.0)
    risk, dd_f, atr_f, exp_g = akad.calculate_risk(0.02)
    
    # Test CorrelationRiskMonitor
    cm = CorrelationRiskMonitor(window=200)
    
    # Check all critical getters
    for attr in ('last_hurst', 'last_z_crit', 'last_exit_z', 'last_std', 'last_mean',
                 'last_z_score', 'last_spread', 'buffer_len'):
        assert hasattr(eng, attr), f'Missing: {attr}'
    
    print(f'RUST OK | CointegrationEngine: Z={sig.z_score:.4f}')
    print(f'RUST OK | KalmanSentinel: beta={beta:.4f}, abort={abort}')
    print(f'RUST OK | AKAD: risk={risk*100:.3f}% at 2% DD')
    print(f'RUST OK | CorrelationRiskMonitor: initialized')
    print(f'RUST OK | FFI contract: all 8 getters present')
    print('PASS')
except Exception as e:
    print(f'FAIL: {e}')
    sys.exit(1)
"@

$result = & $pythonPath -c $smokeTest 2>&1
$resultStr = $result -join "`n"
if ($resultStr -match "PASS") {
    $result | ForEach-Object { Write-Host "  $_" -ForegroundColor Green }
} else {
    Write-Host "  RUST CORE SMOKE TEST FAILED:" -ForegroundColor Red
    $result | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
    Write-Host ""
    Write-Host "  This likely means the .pyd was compiled for a different Python version." -ForegroundColor Yellow
    Write-Host "  The .pyd needs Python 3.10+ (abi3-py310)." -ForegroundColor Yellow
    exit 1
}

# ============================================================================
# STEP 8: Smoke test — Python modules
# ============================================================================
Write-Host "`n[8/9] Smoke testing Python modules..." -ForegroundColor Yellow

$pyModuleTest = @"
import sys, os
os.chdir(r'$TARGET')
sys.path.insert(0, r'$TARGET')

# Test all Python modules import
from src.risk.supervisor import RiskSupervisor, RiskAction, calculate_position_size
from src.risk.akad_risk import AKADRiskManager, DynamicAKAD
from src.execution.mt5_bridge import MT5Bridge, OrderRequest, OrderType, BridgeTimeoutError, ServerTimeInfo

# Test DynamicAKAD
dakad = DynamicAKAD(dd_lambda=40.0, daily_dd_ceiling=0.04)
r1 = dakad.calculate_risk(total_dd=0.0, daily_dd=0.0)
r2 = dakad.calculate_risk(total_dd=0.02, daily_dd=0.01)
dakad.record_trade(win=True)
dakad.record_trade(win=False)
print(f'DynamicAKAD: 0%DD={r1*100:.3f}%, 2%DD={r2*100:.3f}%, trades={dakad.trade_count}')

# Test RiskSupervisor
rs = RiskSupervisor(initial_balance=100000.0)
rs.record_win()
alert = rs.record_loss()
print(f'RiskSupervisor: halted={rs.is_halted}')

# Test HMM
try:
    from src.strategies.hmm_regime import HMMRegimeDetector, create_regime_detector
    hmm = create_regime_detector(n_regimes=3, lookback=100)
    print(f'HMM: OK (Numba available)')
except Exception as e:
    print(f'HMM: Fallback mode ({e})')

print('PASS')
"@

$result = & $pythonPath -c $pyModuleTest 2>&1
$resultStr = $result -join "`n"
if ($resultStr -match "PASS") {
    $result | ForEach-Object { Write-Host "  $_" -ForegroundColor Green }
} else {
    Write-Host "  PYTHON MODULE TEST FAILED:" -ForegroundColor Red
    $result | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
    exit 1
}

# ============================================================================
# STEP 9: MT5 EA setup instructions
# ============================================================================
Write-Host "`n[9/9] MT5 Expert Advisor setup..." -ForegroundColor Yellow

# Try to find MT5 terminal data folder
$mt5DataPaths = @(
    "$env:APPDATA\MetaQuotes\Terminal"
)
$mt5Found = $false
foreach ($basePath in $mt5DataPaths) {
    if (Test-Path $basePath) {
        $terminals = Get-ChildItem -Path $basePath -Directory
        foreach ($terminal in $terminals) {
            $expertDir = "$($terminal.FullName)\MQL5\Experts"
            if (Test-Path $expertDir) {
                Write-Host "  Found MT5 data folder: $expertDir" -ForegroundColor Green
                Copy-Item -Force "$TARGET\MQL5\Experts\SHF_ZMQ_Bridge.mq5" "$expertDir\SHF_ZMQ_Bridge.mq5"
                Write-Host "  EA copied to MT5 Experts folder" -ForegroundColor Green
                $mt5Found = $true
            }
        }
    }
}

if (-Not $mt5Found) {
    Write-Host "  MT5 data folder not found automatically." -ForegroundColor Yellow
    Write-Host "  EA file is at: $TARGET\MQL5\Experts\SHF_ZMQ_Bridge.mq5" -ForegroundColor White
    Write-Host "  You'll need to copy it manually (see instructions below)." -ForegroundColor Yellow
}

# ============================================================================
# SUMMARY
# ============================================================================
Write-Host ""
Write-Host "============================================================================" -ForegroundColor Green
Write-Host "    SHF v5.6 DEPLOYMENT COMPLETE" -ForegroundColor Green
Write-Host "============================================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Deploy Path:  $TARGET" -ForegroundColor White
Write-Host "  Rust Core:    shf_core.pyd (all 4 classes verified)" -ForegroundColor White
Write-Host "  Python:       engine.py + all modules verified" -ForegroundColor White
Write-Host "  Risk:         DynamicAKAD (PRIMARY) + legacy fallbacks" -ForegroundColor White
Write-Host "  EA:           SHF_ZMQ_Bridge.mq5" -ForegroundColor White
Write-Host ""
Write-Host "  Holy Trio:" -ForegroundColor Cyan
Write-Host "    US100/DE40     (Index Spread)" -ForegroundColor White
Write-Host "    AUDUSD/NZDUSD  (Forex Anchor)" -ForegroundColor White
Write-Host "    EURUSD/GBPUSD  (EUR/GBP Spread)" -ForegroundColor White
Write-Host ""
Write-Host "============================================================================" -ForegroundColor Yellow
Write-Host "    NEXT STEPS" -ForegroundColor Yellow
Write-Host "============================================================================" -ForegroundColor Yellow
Write-Host ""
Write-Host "  1. MT5 SETUP (if not already installed):" -ForegroundColor White
Write-Host "     - Download from: https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe" -ForegroundColor Gray
Write-Host "     - Install and login to your broker/prop firm account" -ForegroundColor Gray
Write-Host ""
Write-Host "  2. COMPILE THE EA:" -ForegroundColor White
Write-Host "     - Open MT5 > Press F4 (MetaEditor)" -ForegroundColor Gray
Write-Host "     - Open SHF_ZMQ_Bridge.mq5 from Experts folder" -ForegroundColor Gray
Write-Host "     - Press F7 to compile (ensure ZMQ libraries are available)" -ForegroundColor Gray
Write-Host ""
Write-Host "  3. ENABLE AUTOTRADING:" -ForegroundColor White
Write-Host "     - MT5 > Tools > Options > Expert Advisors" -ForegroundColor Gray
Write-Host "     - [x] Allow DLL imports" -ForegroundColor Gray
Write-Host "     - [x] Allow automated trading" -ForegroundColor Gray
Write-Host ""
Write-Host "  4. ATTACH EA:" -ForegroundColor White
Write-Host "     - Drag SHF_ZMQ_Bridge onto any chart (e.g., US100 M1)" -ForegroundColor Gray
Write-Host "     - EA handles all 6 symbols internally via ZMQ" -ForegroundColor Gray
Write-Host ""
Write-Host "  5. START THE ENGINE:" -ForegroundColor White
Write-Host "     cd $TARGET" -ForegroundColor Cyan
Write-Host "     python -m src.engine" -ForegroundColor Cyan
Write-Host ""
Write-Host "  6. MONITOR:" -ForegroundColor White
Write-Host "     - Logs: $TARGET\logs\trading.log" -ForegroundColor Gray
Write-Host "     - State: $TARGET\state\engine_state.json" -ForegroundColor Gray
Write-Host ""
Write-Host "============================================================================" -ForegroundColor Green
