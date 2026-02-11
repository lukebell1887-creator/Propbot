# ============================================================================
#  SHF v5.6.2 — START ENGINE WITH PRE-FLIGHT CHECKS
#  Paste this ENTIRE block into VPS PowerShell (after FETCH + EA compile)
# ============================================================================

$ErrorActionPreference = "Continue"
Clear-Host

$SHF = "C:\SHF"
$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

# ============================================================================
#  BANNER
# ============================================================================
Write-Host ""
Write-Host "  =========================================================" -ForegroundColor Cyan
Write-Host "       SHF v5.6.2 — Cointegration Pairs Trading Engine" -ForegroundColor Cyan
Write-Host "  =========================================================" -ForegroundColor Cyan
Write-Host "  Started: $timestamp" -ForegroundColor DarkGray
Write-Host ""

# ============================================================================
#  HOLY TRIO DISPLAY
# ============================================================================
Write-Host "  HOLY TRIO (v5.6.2):" -ForegroundColor Yellow
Write-Host "  ---------------------------------------------------------" -ForegroundColor DarkGray
Write-Host "   Pair 1: " -NoNewline -ForegroundColor DarkGray
Write-Host "NAS100 / DAX40     " -NoNewline -ForegroundColor White
Write-Host "HMM=10  " -NoNewline -ForegroundColor Magenta
Write-Host "(Index, H=0.585 trending)" -ForegroundColor DarkGray

Write-Host "   Pair 2: " -NoNewline -ForegroundColor DarkGray
Write-Host "AUDUSD / NZDUSD    " -NoNewline -ForegroundColor White
Write-Host "HMM=100 " -NoNewline -ForegroundColor Green
Write-Host "(Forex, H=0.512 MR)" -ForegroundColor DarkGray

Write-Host "   Pair 3: " -NoNewline -ForegroundColor DarkGray
Write-Host "EURJPY / CHFJPY    " -NoNewline -ForegroundColor White
Write-Host "HMM=100 " -NoNewline -ForegroundColor Green
Write-Host "(JPY Cross, H=0.528 MR)" -ForegroundColor DarkGray
Write-Host "  ---------------------------------------------------------" -ForegroundColor DarkGray
Write-Host ""

# ============================================================================
#  PRE-FLIGHT CHECKS
# ============================================================================
Write-Host "  PRE-FLIGHT CHECKS:" -ForegroundColor Yellow
$checks_passed = 0
$checks_total = 6

# Check 1: Working directory
if (Test-Path $SHF) {
    Write-Host "   [PASS] " -NoNewline -ForegroundColor Green
    Write-Host "Working directory $SHF exists" -ForegroundColor White
    $checks_passed++
} else {
    Write-Host "   [FAIL] " -NoNewline -ForegroundColor Red
    Write-Host "Working directory $SHF NOT FOUND" -ForegroundColor Red
}

# Check 2: shf_core.pyd (Rust engine)
$pydFile = Join-Path $SHF "shf_core.pyd"
if (Test-Path $pydFile) {
    $pydSize = [math]::Round((Get-Item $pydFile).Length / 1MB, 1)
    Write-Host "   [PASS] " -NoNewline -ForegroundColor Green
    Write-Host "shf_core.pyd found (${pydSize} MB)" -ForegroundColor White
    $checks_passed++
} else {
    Write-Host "   [FAIL] " -NoNewline -ForegroundColor Red
    Write-Host "shf_core.pyd NOT FOUND — Rust engine missing!" -ForegroundColor Red
}

# Check 3: engine.py
$engineFile = Join-Path $SHF "src\engine.py"
if (Test-Path $engineFile) {
    $engineDate = (Get-Item $engineFile).LastWriteTime.ToString("yyyy-MM-dd HH:mm")
    Write-Host "   [PASS] " -NoNewline -ForegroundColor Green
    Write-Host "engine.py found (modified: $engineDate)" -ForegroundColor White
    $checks_passed++
} else {
    Write-Host "   [FAIL] " -NoNewline -ForegroundColor Red
    Write-Host "engine.py NOT FOUND" -ForegroundColor Red
}

# Check 4: mt5_bridge.py
$bridgeFile = Join-Path $SHF "src\execution\mt5_bridge.py"
if (Test-Path $bridgeFile) {
    Write-Host "   [PASS] " -NoNewline -ForegroundColor Green
    Write-Host "mt5_bridge.py found" -ForegroundColor White
    $checks_passed++
} else {
    Write-Host "   [FAIL] " -NoNewline -ForegroundColor Red
    Write-Host "mt5_bridge.py NOT FOUND" -ForegroundColor Red
}

# Check 5: HMM regime detector
$hmmFile = Join-Path $SHF "src\strategies\hmm_regime.py"
if (Test-Path $hmmFile) {
    Write-Host "   [PASS] " -NoNewline -ForegroundColor Green
    Write-Host "hmm_regime.py found (per-pair HMM holds)" -ForegroundColor White
    $checks_passed++
} else {
    Write-Host "   [FAIL] " -NoNewline -ForegroundColor Red
    Write-Host "hmm_regime.py NOT FOUND" -ForegroundColor Red
}

# Check 6: Python available
$pyVer = python --version 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Host "   [PASS] " -NoNewline -ForegroundColor Green
    Write-Host "$pyVer" -ForegroundColor White
    $checks_passed++
} else {
    Write-Host "   [FAIL] " -NoNewline -ForegroundColor Red
    Write-Host "Python not found in PATH" -ForegroundColor Red
}

# Check 7 (info only): MT5 process
$mt5 = Get-Process terminal64 -ErrorAction SilentlyContinue
if ($mt5) {
    Write-Host "   [INFO] " -NoNewline -ForegroundColor Cyan
    Write-Host "MT5 Terminal running (PID: $($mt5.Id))" -ForegroundColor White
} else {
    Write-Host "   [WARN] " -NoNewline -ForegroundColor Yellow
    Write-Host "MT5 Terminal not detected — start MT5 and attach EA first!" -ForegroundColor Yellow
}

# Check 8 (info only): Port 5555
$portCheck = netstat -an 2>$null | Select-String ":5555 "
if ($portCheck) {
    Write-Host "   [WARN] " -NoNewline -ForegroundColor Yellow
    Write-Host "Port 5555 already in use — old engine still running?" -ForegroundColor Yellow
} else {
    Write-Host "   [INFO] " -NoNewline -ForegroundColor Cyan
    Write-Host "Port 5555 available" -ForegroundColor White
}

Write-Host ""
Write-Host "  ---------------------------------------------------------" -ForegroundColor DarkGray

# Pre-flight result
if ($checks_passed -eq $checks_total) {
    Write-Host "  RESULT: " -NoNewline -ForegroundColor Green
    Write-Host "$checks_passed/$checks_total PASSED — all systems go!" -ForegroundColor Green
} else {
    Write-Host "  RESULT: " -NoNewline -ForegroundColor Red
    Write-Host "$checks_passed/$checks_total PASSED — some checks failed!" -ForegroundColor Red
    Write-Host ""
    Write-Host "  Fix the issues above before continuing." -ForegroundColor Red
    Write-Host "  Press Ctrl+C to abort, or Enter to continue anyway..." -ForegroundColor Yellow
    Read-Host
}

Write-Host ""

# ============================================================================
#  SYSTEM CONFIG SUMMARY
# ============================================================================
Write-Host "  SYSTEM CONFIG:" -ForegroundColor Yellow
Write-Host "   Dynamic Z Entry:  base=2.0, gamma=6.0 (Hurst-adaptive)" -ForegroundColor DarkGray
Write-Host "   Dynamic Z Exit:   base=0.5, gamma=2.0 (Hurst-adaptive)" -ForegroundColor DarkGray
Write-Host "   Dynamic AKAD:     lam=40, P_ruin=1e-4, DD_ceil=4%" -ForegroundColor DarkGray
Write-Host "   Ghost Stop:       daily=4%, max=9%" -ForegroundColor DarkGray
Write-Host "   Dwell:            60s base, range [30s-300s]" -ForegroundColor DarkGray
Write-Host "   M1 Bar Mode:      signals on bar close only" -ForegroundColor DarkGray
Write-Host "   Pre-warm:         768 M1 bars from broker" -ForegroundColor DarkGray
Write-Host "   TCP Bridge:       localhost:5555 (native socket)" -ForegroundColor DarkGray
Write-Host ""

# ============================================================================
#  LAUNCH ENGINE
# ============================================================================
Write-Host "  =========================================================" -ForegroundColor Green
Write-Host "       LAUNCHING ENGINE — Waiting for EA connection..." -ForegroundColor Green
Write-Host "  =========================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Tip: Make sure the EA is attached to a chart in MT5." -ForegroundColor DarkGray
Write-Host "  The engine will show PRE-WARM progress, then start trading." -ForegroundColor DarkGray
Write-Host "  Status logs every 5 minutes. Press Ctrl+C to stop." -ForegroundColor DarkGray
Write-Host ""
Write-Host "  ------- ENGINE OUTPUT BELOW -------" -ForegroundColor DarkGray
Write-Host ""

Set-Location $SHF
python -m src.engine
