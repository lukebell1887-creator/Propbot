# ============================================================================
#  SHF v5.6.2 — VPS FETCH FROM GITHUB
#  Paste this ENTIRE block into VPS PowerShell
# ============================================================================

$ErrorActionPreference = "Stop"
Clear-Host

Write-Host ""
Write-Host "  ╔══════════════════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "  ║        SHF v5.6.2 — FETCH LATEST FROM GITHUB           ║" -ForegroundColor Cyan
Write-Host "  ║  New Holy Trio: NAS100/DAX40 | AUD/NZD | EURJPY/CHFJPY ║" -ForegroundColor Cyan
Write-Host "  ╚══════════════════════════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""

$SHF = "C:\SHF"
$REPO = "https://github.com/lukebell1887-creator/Propbot.git"
$TEMP = "C:\SHF_UPDATE_TEMP"

# --- Step 1: Kill any running engine ---
Write-Host "  [1/5] Stopping any running engine..." -ForegroundColor Yellow
$procs = Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like "*engine*" -or $_.MainWindowTitle -like "*engine*" }
if ($procs) {
    $procs | Stop-Process -Force
    Write-Host "        Engine stopped" -ForegroundColor Green
    Start-Sleep -Seconds 2
} else {
    Write-Host "        No engine running" -ForegroundColor DarkGray
}

# --- Step 2: Backup shf_core.pyd (compiled Rust — cannot be rebuilt on VPS) ---
Write-Host "  [2/5] Backing up shf_core.pyd..." -ForegroundColor Yellow
$pydPath = Join-Path $SHF "shf_core.pyd"
$pydBackup = "C:\shf_core_backup.pyd"
if (Test-Path $pydPath) {
    Copy-Item -Force $pydPath $pydBackup
    $size = (Get-Item $pydPath).Length / 1MB
    Write-Host "        Backed up ($([math]::Round($size,1)) MB)" -ForegroundColor Green
} else {
    Write-Host "        WARNING: shf_core.pyd not found at $pydPath" -ForegroundColor Red
    Write-Host "        The Rust DLL must exist — check previous deployment" -ForegroundColor Red
}

# --- Step 3: Clone/Pull from GitHub ---
Write-Host "  [3/5] Fetching latest from GitHub..." -ForegroundColor Yellow

if (Test-Path $TEMP) { Remove-Item -Recurse -Force $TEMP }

git clone --depth 1 $REPO $TEMP 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "        Git clone FAILED — check network/credentials" -ForegroundColor Red
    exit 1
}
$commitHash = (git -C $TEMP log --oneline -1) 2>&1
Write-Host "        Cloned: $commitHash" -ForegroundColor Green

# --- Step 4: Copy files to C:\SHF ---
Write-Host "  [4/5] Deploying files to $SHF..." -ForegroundColor Yellow

# Ensure directory structure
$dirs = @("$SHF\src", "$SHF\src\execution", "$SHF\src\risk", "$SHF\src\strategies", "$SHF\logs", "$SHF\state")
foreach ($d in $dirs) { New-Item -ItemType Directory -Force -Path $d | Out-Null }

# Copy Python source files
$files = @(
    @("src\__init__.py",              "src\__init__.py"),
    @("src\engine.py",                "src\engine.py"),
    @("src\execution\__init__.py",    "src\execution\__init__.py"),
    @("src\execution\mt5_bridge.py",  "src\execution\mt5_bridge.py"),
    @("src\risk\__init__.py",         "src\risk\__init__.py"),
    @("src\risk\akad_risk.py",        "src\risk\akad_risk.py"),
    @("src\risk\supervisor.py",       "src\risk\supervisor.py"),
    @("src\strategies\__init__.py",   "src\strategies\__init__.py"),
    @("src\strategies\hmm_regime.py", "src\strategies\hmm_regime.py")
)

$copied = 0
foreach ($f in $files) {
    $src = Join-Path $TEMP $f[0]
    $dst = Join-Path $SHF $f[1]
    if (Test-Path $src) {
        Copy-Item -Force $src $dst
        $copied++
    } else {
        Write-Host "        MISSING: $($f[0])" -ForegroundColor Red
    }
}
Write-Host "        $copied Python files deployed" -ForegroundColor Green

# Copy EA file (for user to compile via F4/F7)
$eaSrc = Join-Path $TEMP "MQL5\Experts\SHF_Bridge.mq5"
if (Test-Path $eaSrc) {
    # Try common MT5 data paths
    $mt5Paths = @(
        "$env:APPDATA\MetaQuotes\Terminal",
        "C:\Program Files\MetaTrader 5\MQL5\Experts",
        "C:\Program Files\FivePercentOnline MT5 Terminal\MQL5\Experts"
    )
    # Also copy to SHF for reference
    New-Item -ItemType Directory -Force -Path "$SHF\MQL5\Experts" | Out-Null
    Copy-Item -Force $eaSrc "$SHF\MQL5\Experts\SHF_Bridge.mq5"
    Write-Host "        EA copied to $SHF\MQL5\Experts\" -ForegroundColor Green
    Write-Host "        >> Open MT5 MetaEditor (F4), open SHF_Bridge.mq5, press F7 to compile <<" -ForegroundColor Yellow
}

# Restore shf_core.pyd
if (Test-Path $pydBackup) {
    Copy-Item -Force $pydBackup $pydPath
    Write-Host "        shf_core.pyd restored" -ForegroundColor Green
}

# --- Step 5: Cleanup ---
Write-Host "  [5/5] Cleaning up..." -ForegroundColor Yellow
Remove-Item -Recurse -Force $TEMP -ErrorAction SilentlyContinue
if (Test-Path $pydBackup) { Remove-Item -Force $pydBackup -ErrorAction SilentlyContinue }
Write-Host "        Done" -ForegroundColor Green

# --- Summary ---
Write-Host ""
Write-Host "  ╔══════════════════════════════════════════════════════════╗" -ForegroundColor Green
Write-Host "  ║                  FETCH COMPLETE                         ║" -ForegroundColor Green
Write-Host "  ╚══════════════════════════════════════════════════════════╝" -ForegroundColor Green
Write-Host ""
Write-Host "  Files deployed to: $SHF" -ForegroundColor White
Write-Host "  Commit: $commitHash" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  NEXT STEPS:" -ForegroundColor Yellow
Write-Host "    1. Open MT5 → MetaEditor (F4)" -ForegroundColor White
Write-Host "    2. Open SHF_Bridge.mq5 → Compile (F7)" -ForegroundColor White
Write-Host "    3. Attach EA to any chart (EURJPY or similar)" -ForegroundColor White
Write-Host "    4. Run the engine (paste RUN script)" -ForegroundColor White
Write-Host ""
