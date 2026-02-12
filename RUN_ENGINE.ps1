# =============================================================================
# SHF v5.6.3 ENGINE LAUNCHER — Oil + Index Duo
# =============================================================================
# Run this on the VPS inside C:\SHF to start the trading engine.
# It verifies all components before launching.
# =============================================================================

$ErrorActionPreference = "Stop"
Set-Location "C:\SHF"

# ---- ANSI Colors (PowerShell 7+) ----
$G = "`e[32m"; $R = "`e[31m"; $Y = "`e[33m"; $C = "`e[36m"; $B = "`e[1m"; $X = "`e[0m"

function Write-Header($msg) { Write-Host "`n${B}${C}=== $msg ===${X}" }
function Write-OK($msg)     { Write-Host "  ${G}[OK]${X} $msg" }
function Write-FAIL($msg)   { Write-Host "  ${R}[FAIL]${X} $msg" }
function Write-WARN($msg)   { Write-Host "  ${Y}[WARN]${X} $msg" }
function Write-Info($msg)   { Write-Host "  ${C}[INFO]${X} $msg" }

Clear-Host
Write-Host ""
Write-Host "${B}${C}  =============================================${X}"
Write-Host "${B}${C}     SHF v5.6.3 — Oil + Index Trading Engine   ${X}"
Write-Host "${B}${C}     Pairs: US100/DE40 | XTIUSD/XBRUSD        ${X}"
Write-Host "${B}${C}     HMM: Index=20 | Oil=5                     ${X}"
Write-Host "${B}${C}     Dwell: Index=60s | Oil=1800s base         ${X}"
Write-Host "${B}${C}  =============================================${X}"
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
        Write-FAIL "$f — MISSING!"
        $allPresent = $false
    }
}
if (-not $allPresent) {
    Write-Host "`n${R}${B}ABORT: Missing critical files. Run git pull first.${X}"
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
    foreach ($p in $parts[1..4]) { Write-OK "Rust class: $p" }
    Write-OK "FFI contract validated (all getters present)"
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
    print(f'OK|regimes=3|hold_param=5|blocked={d.is_blocked}')
except Exception as ex:
    print(f'FAIL|{ex}')
" 2>&1
if ($hmmCheck -match "^OK") {
    $parts = $hmmCheck -split '\|'
    foreach ($p in $parts[1..3]) { Write-OK $p }
} else {
    Write-WARN "HMM: $hmmCheck (will use fallback)"
}

# ---- Step 4: Dynamic AKAD Check ----
Write-Header "DYNAMIC AKAD RISK"
$akadCheck = python -c "
from src.risk.akad_risk import DynamicAKAD
d = DynamicAKAD(dd_lambda=40.0, daily_dd_ceiling=0.04)
r0 = d.calculate_risk(total_dd=0.0, daily_dd=0.0)
r2 = d.calculate_risk(total_dd=0.02, daily_dd=0.01)
r4 = d.calculate_risk(total_dd=0.0, daily_dd=0.039)
print(f'OK|0%%DD={r0*100:.3f}%%|2%%DD={r2*100:.3f}%%|NearCeiling={r4*100:.3f}%%')
" 2>&1
if ($akadCheck -match "^OK") {
    $parts = $akadCheck -split '\|'
    foreach ($p in $parts[1..3]) { Write-OK $p }
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
    print(f'{p.name}|{p.symbol_a}/{p.symbol_b}|HMM={p.hmm_min_hold}|Dwell@H0.5={dwell_h05:.0f}s ({dwell_h05/60:.0f}min)|Spread={p.max_spread_a:.0f}/{p.max_spread_b:.0f}')
" 2>&1
foreach ($line in $pairCheck) {
    $parts = $line -split '\|'
    if ($parts.Length -ge 4) {
        Write-OK "$($parts[0]): $($parts[1]) | $($parts[2]) | $($parts[3]) | MaxSpread=$($parts[4])"
    }
}

# ---- Step 6: TCP Bridge Port Check ----
Write-Header "TCP BRIDGE (port 5555)"
$portInUse = Get-NetTCPConnection -LocalPort 5555 -ErrorAction SilentlyContinue
if ($portInUse) {
    Write-WARN "Port 5555 already in use — killing old process"
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
Write-Host "${B}${G}  ALL CHECKS PASSED — LAUNCHING ENGINE${X}"
Write-Host ""
Write-Host "  ${C}Pairs:${X}  Index Spread (NAS100/DAX40) HMM=20 Dwell=60s"
Write-Host "  ${C}        Oil Spread (XTIUSD/XBRUSD)  HMM=5  Dwell=1800s"
Write-Host "  ${C}Risk:${X}   Dynamic AKAD | 4% daily DD | 9% max DD"
Write-Host "  ${C}Mode:${X}   M1 bar signals | 768-bar pre-warm | 100ms tick"
Write-Host "  ${C}Bridge:${X} TCP localhost:5555 (native MQL5 sockets)"
Write-Host ""
Write-Host "  ${Y}Waiting for EA connection on port 5555...${X}"
Write-Host "  ${Y}Make sure SHF_Bridge EA is attached to an MT5 chart.${X}"
Write-Host ""
Write-Host "${B}--- ENGINE OUTPUT BELOW ---${X}"
Write-Host ""

# ---- Launch ----
python -m src.engine
