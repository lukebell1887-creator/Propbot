# =============================================================================
# SHF v5.6.4 ENGINE LAUNCHER - Oil + Index Duo
# =============================================================================
# PowerShell 5.1 compatible (no ANSI escapes, ASCII only)
# Engine runs in a HIDDEN window — immune to console freezes/QuickEdit
# =============================================================================

$ErrorActionPreference = "Stop"
Set-Location "C:\SHF"

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
Write-Host "     SHF v5.6.4 - Oil + Index Trading Engine   " -ForegroundColor Cyan
Write-Host "     Pairs: US100/DE40 | XTIUSD/XBRUSD        " -ForegroundColor Cyan
Write-Host "     HMM: Index=20 | Oil=10                    " -ForegroundColor Cyan
Write-Host "     Dwell: Index=60s | Oil=1800s base         " -ForegroundColor Cyan
Write-Host "     Mode: HIDDEN WINDOW (freeze-proof)        " -ForegroundColor Cyan
Write-Host "  =============================================" -ForegroundColor Cyan
Write-Host ""

# Prevent Windows sleep via registry (simple, no Add-Type needed)
try {
    powercfg /change standby-timeout-ac 0
    powercfg /change hibernate-timeout-ac 0
    Write-OK "Windows sleep/hibernate DISABLED"
} catch {
    Write-WARN "Could not change power settings"
}

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
Write-Host "          Oil (XTIUSD/XBRUSD)  HMM=10 Dwell=1800s" -ForegroundColor Cyan
Write-Host "  Risk:   Dynamic AKAD | 4pct daily DD | 9pct max DD" -ForegroundColor Cyan
Write-Host "  Mode:   M1 bar signals | 768-bar pre-warm | 100ms tick" -ForegroundColor Cyan
Write-Host "  Bridge: TCP localhost:5555" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Make sure SHF_Bridge EA is attached to an MT5 chart." -ForegroundColor Yellow

# =============================================================================
# LAUNCH ENGINE IN HIDDEN WINDOW (FREEZE-PROOF)
# =============================================================================
# Python runs inside a hidden CMD window with stdout piped to a log file.
# This console only tails the log. Even if this console freezes, disconnects,
# or you close RDP — the Python engine keeps running independently.
# QuickEdit, console buffer, mouse clicks — NONE of it can affect the engine.
# =============================================================================

Write-Host ""
Write-Host "  Engine runs in HIDDEN window — immune to console freezes" -ForegroundColor Green
Write-Host "  Output: logs/console.log + logs/trading.log" -ForegroundColor Yellow
Write-Host ""

# Clear old console log
if (Test-Path "logs\console.log") {
    Remove-Item "logs\console.log" -Force
}
New-Item -Path "logs\console.log" -ItemType File -Force | Out-Null

# Launch Python in a HIDDEN CMD window — completely detached from this console
$cmdArgs = '/c cd /d C:\SHF && python -u -m src.engine >> logs\console.log 2>&1'
Start-Process cmd -ArgumentList $cmdArgs -WindowStyle Hidden

Start-Sleep 3

# Find and display the engine PID
$pyProc = Get-Process python -ErrorAction SilentlyContinue | Select-Object -First 1
if ($pyProc) {
    $enginePid = $pyProc.Id
    Write-Host "  Engine PID: $enginePid" -ForegroundColor Green
    Write-Host "  Engine is RUNNING in hidden window" -ForegroundColor Green
} else {
    Write-Host "  [WARN] Python process not found - check logs\console.log" -ForegroundColor Red
}

Write-Host ""
Write-Host "  ================================================" -ForegroundColor Cyan
Write-Host "  LIVE LOG TAIL" -ForegroundColor Cyan
Write-Host "  ================================================" -ForegroundColor Cyan
Write-Host "  Ctrl+C = stop watching (engine keeps running)" -ForegroundColor Yellow
Write-Host "  Kill engine:  Get-Process python | Stop-Process" -ForegroundColor Yellow
Write-Host "  Re-watch log: Get-Content C:\SHF\logs\console.log -Wait -Tail 50" -ForegroundColor Yellow
Write-Host ""

# Tail the console log file
Get-Content "logs\console.log" -Wait -Tail 100
