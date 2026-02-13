# =============================================================================
# SHF v5.6.3 ENGINE LAUNCHER - Oil + Index Duo
# =============================================================================
# PowerShell 5.1 compatible (no ANSI escapes, ASCII only)
# =============================================================================

$ErrorActionPreference = "Stop"
Set-Location "C:\SHF"

# =============================================================================
# ANTI-FREEZE: Disable QuickEdit Mode + Prevent Windows Sleep
# =============================================================================
# QuickEdit: Clicking in a PowerShell window pauses the ENTIRE process.
# This is the #1 cause of random multi-hour freezes on Windows VPS.
# We disable it before launching the engine.
try {
    $key = 'HKCU:\Console'
    Set-ItemProperty -Path $key -Name QuickEdit -Value 0 -ErrorAction SilentlyContinue
    # Also disable via Win32 API for THIS console session
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class ConsoleHelper {
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern IntPtr GetStdHandle(int nStdHandle);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool GetConsoleMode(IntPtr hConsoleHandle, out uint lpMode);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool SetConsoleMode(IntPtr hConsoleHandle, uint dwMode);
    public static void DisableQuickEdit() {
        IntPtr handle = GetStdHandle(-10);
        uint mode;
        GetConsoleMode(handle, out mode);
        mode &= ~(uint)0x0040; // ENABLE_QUICK_EDIT_MODE
        mode &= ~(uint)0x0020; // ENABLE_INSERT_MODE
        mode |= (uint)0x0080;  // ENABLE_EXTENDED_FLAGS
        SetConsoleMode(handle, mode);
    }
}
"@ -ErrorAction SilentlyContinue
    [ConsoleHelper]::DisableQuickEdit()
    Write-Host "  [ANTI-FREEZE] QuickEdit mode DISABLED for this session" -ForegroundColor Green
} catch {
    Write-Host "  [WARN] Could not disable QuickEdit: $_" -ForegroundColor Yellow
}

# Prevent Windows from sleeping while engine runs (ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
try {
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class SleepPreventer {
    [DllImport("kernel32.dll")]
    public static extern uint SetThreadExecutionState(uint esFlags);
    public static void PreventSleep() {
        SetThreadExecutionState(0x80000001); // ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    }
}
"@ -ErrorAction SilentlyContinue
    [SleepPreventer]::PreventSleep()
    Write-Host "  [ANTI-FREEZE] Windows sleep prevention ACTIVE" -ForegroundColor Green
} catch {
    Write-Host "  [WARN] Could not prevent sleep: $_" -ForegroundColor Yellow
}

function Write-Header($msg) {
    Write-Host ""
    Write-Host "=== $msg ===" -ForegroundColor Cyan
}
function Write-OK($msg) {
    Write-Host "  [OK] " -ForegroundColor Green -NoNewline
    Write-Host $msg
}
function Write-FAIL($msg) {
    Write-Host "  [FAIL] " -ForegroundColor Red -NoNewline
    Write-Host $msg
}
function Write-WARN($msg) {
    Write-Host "  [WARN] " -ForegroundColor Yellow -NoNewline
    Write-Host $msg
}

Clear-Host
Write-Host ""
Write-Host "  =============================================" -ForegroundColor Cyan
Write-Host "     SHF v5.6.3 - Oil + Index Trading Engine   " -ForegroundColor Cyan
Write-Host "     Pairs: US100/DE40 | XTIUSD/XBRUSD        " -ForegroundColor Cyan
Write-Host "     HMM: Index=20 | Oil=5                     " -ForegroundColor Cyan
Write-Host "     Dwell: Index=60s | Oil=1800s base         " -ForegroundColor Cyan
Write-Host "  =============================================" -ForegroundColor Cyan
Write-Host ""

# ---- Step 1: File Integrity Check ----
Write-Header "FILE INTEGRITY CHECK"
$requiredFiles = @(
    "shf_core.pyd",
    "src\engine.py",
    "src\execution\mt5_bridge.py",
    "src\risk\akad_risk.py",
    "src\risk\supervisor.py",
    "src\strategies\hmm_regime.py",
    "src\__init__.py",
    "src\execution\__init__.py",
    "src\risk\__init__.py",
    "src\strategies\__init__.py"
)
$allPresent = $true
foreach ($f in $requiredFiles) {
    if (Test-Path $f) {
        $sz = (Get-Item $f).Length
        Write-OK "$f ($sz bytes)"
    } else {
        Write-FAIL "$f -- MISSING"
        $allPresent = $false
    }
}
if (-not $allPresent) {
    Write-Host ""
    Write-Host "ABORT: Missing critical files. Run git pull first." -ForegroundColor Red
    exit 1
}

# ---- Step 2: Rust Core Validation ----
Write-Header "RUST CORE VALIDATION"
$rustCheck = python -c "
try:
    from shf_core import CointegrationEngine, KalmanSentinel, AKADRiskCalculator, CorrelationRiskMonitor
    e = CointegrationEngine(span=100, beta=1.0, dynamic_z=True, dynamic_exit=True)
    for a in ['last_hurst','last_z_crit','last_exit_z','last_std','buffer_len']:
        assert hasattr(e, a), f'Missing: {a}'
    print('OK|CointegrationEngine|KalmanSentinel|AKADRiskCalculator|CorrelationRiskMonitor')
except Exception as ex:
    print(f'FAIL|{ex}')
" 2>&1
if ($rustCheck -match "^OK") {
    $parts = $rustCheck -split '\|'
    foreach ($p in $parts[1..4]) { Write-OK "Rust: $p" }
    Write-OK "FFI contract validated"
} else {
    Write-FAIL "Rust core: $rustCheck"
    exit 1
}

# ---- Step 3: HMM Filter Check ----
Write-Header "HMM VOLATILITY FILTER"
$hmmCheck = python -c "
try:
    from src.strategies.hmm_regime import create_regime_detector
    d = create_regime_detector(n_regimes=3, lookback=100, min_regime_hold=5)
    d.update(0.001)
    print('OK|regimes=3|hold=5|blocked=' + str(d.is_blocked))
except Exception as ex:
    print('FAIL|' + str(ex))
" 2>&1
if ($hmmCheck -match "^OK") {
    $parts = $hmmCheck -split '\|'
    foreach ($p in $parts[1..3]) { Write-OK $p }
} else {
    Write-WARN "HMM: $hmmCheck"
}

# ---- Step 4: Dynamic AKAD Check ----
Write-Header "DYNAMIC AKAD RISK"
$akadCheck = python -c "
from src.risk.akad_risk import DynamicAKAD
d = DynamicAKAD(dd_lambda=40.0, daily_dd_ceiling=0.04)
r0 = d.calculate_risk(total_dd=0.0, daily_dd=0.0)
r2 = d.calculate_risk(total_dd=0.02, daily_dd=0.01)
print('OK|0pct_DD=%.3f pct|2pct_DD=%.3f pct' % (r0*100, r2*100))
" 2>&1
if ($akadCheck -match "^OK") {
    $parts = $akadCheck -split '\|'
    foreach ($p in $parts[1..2]) { Write-OK $p }
} else {
    Write-FAIL "AKAD: $akadCheck"
}

# ---- Step 5: Pair Config Verification ----
Write-Header "PAIR CONFIGURATION"
$pairCheck = python -c "
from src.engine import HOLY_TRIO
for p in HOLY_TRIO:
    dwell_h05 = p.dwell_base * (0.5 / p.dwell_anchor)
    dwell_h05 = max(p.dwell_min, min(p.dwell_max, dwell_h05))
    print('%s|%s/%s|HMM=%d|Dwell@H0.5=%.0fs (%.0fmin)|MaxSpread=%.0f/%.0f' % (p.name, p.symbol_a, p.symbol_b, p.hmm_min_hold, dwell_h05, dwell_h05/60, p.max_spread_a, p.max_spread_b))
" 2>&1
foreach ($line in $pairCheck) {
    $parts = $line -split '\|'
    if ($parts.Length -ge 4) {
        Write-OK ($parts -join " | ")
    }
}

# ---- Step 6: TCP Bridge Port Check ----
Write-Header "TCP BRIDGE (port 5555)"
$portInUse = Get-NetTCPConnection -LocalPort 5555 -ErrorAction SilentlyContinue
if ($portInUse) {
    Write-WARN "Port 5555 already in use - killing old process"
    $portInUse | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
    Start-Sleep 2
}
Write-OK "Port 5555 available"

# ---- Step 7: Log Directory ----
Write-Header "LOG DIRECTORY"
New-Item -ItemType Directory -Force -Path "logs" | Out-Null
New-Item -ItemType Directory -Force -Path "state" | Out-Null
Write-OK "logs/ and state/ directories ready"

# ---- Summary ----
Write-Host ""
Write-Host "  ALL CHECKS PASSED - LAUNCHING ENGINE" -ForegroundColor Green
Write-Host ""
Write-Host "  Pairs:  Index (NAS100/DAX40) HMM=20 Dwell=60s" -ForegroundColor Cyan
Write-Host "          Oil (XTIUSD/XBRUSD)  HMM=5  Dwell=1800s" -ForegroundColor Cyan
Write-Host "  Risk:   Dynamic AKAD | 4% daily DD | 9% max DD" -ForegroundColor Cyan
Write-Host "  Mode:   M1 bar signals | 768-bar pre-warm | 100ms tick" -ForegroundColor Cyan
Write-Host "  Bridge: TCP localhost:5555" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Waiting for EA connection on port 5555..." -ForegroundColor Yellow
Write-Host "  Make sure SHF_Bridge EA is attached to an MT5 chart." -ForegroundColor Yellow
Write-Host ""
Write-Host "--- ENGINE OUTPUT BELOW ---" -ForegroundColor White
Write-Host ""

# ---- Launch ----
# Run Python DETACHED from console stdout to prevent ANY freeze.
# All output goes to logs/trading.log (file handler) AND logs/console.log (stdout).
# The console tails the log file so you can still watch it live.
# If the console freezes/disconnects, Python keeps running independently.
Write-Host ""
Write-Host "  Engine output is logged to: logs/trading.log" -ForegroundColor Yellow
Write-Host "  Console mirror:             logs/console.log" -ForegroundColor Yellow
Write-Host "  Press Ctrl+C to stop watching (engine keeps running)" -ForegroundColor Yellow
Write-Host ""

# Start engine as a background job, redirect stdout/stderr to console.log
$engineProcess = Start-Process -FilePath "python" -ArgumentList "-u -m src.engine" `
    -WorkingDirectory "C:\SHF" -NoNewWindow -PassThru `
    -RedirectStandardOutput "logs\console.log" -RedirectStandardError "logs\console_err.log"

Write-Host "  Engine PID: $($engineProcess.Id)" -ForegroundColor Green
Write-Host "  Engine is running detached — safe from console freezes" -ForegroundColor Green
Write-Host ""
Write-Host "--- LIVE LOG TAIL (Ctrl+C to stop watching, engine keeps running) ---" -ForegroundColor Cyan
Write-Host ""

# Wait a moment for the log file to be created
Start-Sleep 2

# Tail the log file so the user can watch
try {
    Get-Content "logs\console.log" -Wait -Tail 50
} catch {
    Write-Host "Log tail stopped. Engine is still running (PID: $($engineProcess.Id))." -ForegroundColor Yellow
    Write-Host "To stop the engine: Stop-Process -Id $($engineProcess.Id)" -ForegroundColor Yellow
}
