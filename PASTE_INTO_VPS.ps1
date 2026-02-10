# ============================================================================
# SHF PHASE 2.7 - ONE-SHOT VPS DEPLOYMENT
# ============================================================================
# INSTRUCTIONS:
# 1. RDP to 78.141.192.253 (Administrator / !Ww8+@!zr2A!*5,f)
# 2. Open PowerShell as Administrator
# 3. Copy-paste this ENTIRE script
# ============================================================================

$ErrorActionPreference = "Continue"
Write-Host "============================================================================" -ForegroundColor Cyan
Write-Host "    SHF PHASE 2.7 - VULTR LONDON DEPLOYMENT" -ForegroundColor Cyan
Write-Host "    Target: 78.141.192.253 | 5%ers Challenge Ready" -ForegroundColor Cyan
Write-Host "============================================================================" -ForegroundColor Cyan

# Bypass execution policy for this session
Set-ExecutionPolicy Bypass -Scope Process -Force

# Create deployment directory
$SHF_DIR = "C:\SHF"
Write-Host "`n[1/10] Creating $SHF_DIR..." -ForegroundColor Yellow
New-Item -ItemType Directory -Path $SHF_DIR -Force | Out-Null

# Disable IE Enhanced Security
Write-Host "[2/10] Disabling IE Enhanced Security..." -ForegroundColor Yellow
$AdminKey = "HKLM:\SOFTWARE\Microsoft\Active Setup\Installed Components\{A509B1A7-37EF-4b3f-8CFC-4F3A74704073}"
$UserKey = "HKLM:\SOFTWARE\Microsoft\Active Setup\Installed Components\{A509B1A8-37EF-4b3f-8CFC-4F3A74704073}"
Set-ItemProperty -Path $AdminKey -Name "IsInstalled" -Value 0 -ErrorAction SilentlyContinue
Set-ItemProperty -Path $UserKey -Name "IsInstalled" -Value 0 -ErrorAction SilentlyContinue
Write-Host "    IE ESC Disabled" -ForegroundColor Green

# Install Chocolatey
Write-Host "[3/10] Installing Chocolatey..." -ForegroundColor Yellow
if (!(Get-Command choco -ErrorAction SilentlyContinue)) {
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072
    iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))
    $env:Path += ";C:\ProgramData\chocolatey\bin"
}
Write-Host "    Chocolatey Ready" -ForegroundColor Green

# Refresh PATH
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")

# Install Git
Write-Host "[4/10] Installing Git..." -ForegroundColor Yellow
choco install git -y --no-progress 2>&1 | Out-Null
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
Write-Host "    Git Installed" -ForegroundColor Green

# Install Python 3.10
Write-Host "[5/10] Installing Python 3.10..." -ForegroundColor Yellow
choco install python310 -y --no-progress 2>&1 | Out-Null
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
Write-Host "    Python 3.10 Installed" -ForegroundColor Green

# Install Visual Studio Build Tools (required for Rust)
Write-Host "[6/10] Installing VS Build Tools (C++)... (5-10 min)" -ForegroundColor Yellow
$vsBuildToolsUrl = "https://aka.ms/vs/17/release/vs_buildtools.exe"
$vsBuildToolsPath = "$env:TEMP\vs_buildtools.exe"
Invoke-WebRequest -Uri $vsBuildToolsUrl -OutFile $vsBuildToolsPath -UseBasicParsing
Start-Process -Wait -FilePath $vsBuildToolsPath -ArgumentList "--quiet", "--wait", "--norestart", "--nocache", "--add", "Microsoft.VisualStudio.Workload.VCTools", "--add", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "--add", "Microsoft.VisualStudio.Component.Windows10SDK.19041"
Write-Host "    VS Build Tools Installed" -ForegroundColor Green

# Install Rust
Write-Host "[7/10] Installing Rust (MSVC)..." -ForegroundColor Yellow
$rustupUrl = "https://win.rustup.rs/x86_64"
$rustupPath = "$env:TEMP\rustup-init.exe"
Invoke-WebRequest -Uri $rustupUrl -OutFile $rustupPath -UseBasicParsing
Start-Process -Wait -FilePath $rustupPath -ArgumentList "-y", "--default-toolchain", "stable-x86_64-pc-windows-msvc"
$env:Path += ";$env:USERPROFILE\.cargo\bin"
Write-Host "    Rust Installed" -ForegroundColor Green

# Download SHF package from your location
Write-Host "[8/10] Waiting for SHF files..." -ForegroundColor Yellow
Write-Host @"

============================================================================
    MANUAL STEP: UPLOAD SHF FILES
============================================================================
    Please copy your 'SHF_Phase27_Deploy.zip' (748 MB) to $SHF_DIR
    
    Option 1: Use OneDrive/Google Drive to upload
    Option 2: Use USB drive  
    Option 3: SCP/SFTP transfer
    
    After copying, extract to $SHF_DIR and press ENTER to continue...
============================================================================
"@ -ForegroundColor Magenta
Read-Host "Press ENTER when files are in $SHF_DIR"

# Compile Rust
Write-Host "[9/10] Compiling Rust Safety Core..." -ForegroundColor Yellow
if (Test-Path "$SHF_DIR\rust_core\Cargo.toml") {
    Push-Location "$SHF_DIR\rust_core"
    
    # Refresh PATH to get Rust
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User") + ";$env:USERPROFILE\.cargo\bin"
    
    cargo build --release 2>&1 | Tee-Object -Variable buildOutput
    
    if ($LASTEXITCODE -eq 0) {
        Write-Host "    Rust Core COMPILED SUCCESSFULLY" -ForegroundColor Green
        
        # Verify Ghost Stop
        $riskRs = Get-Content "$SHF_DIR\rust_core\src\risk.rs" -Raw
        if ($riskRs -match "DAILY_DD_GHOST.*=.*0\.040") {
            Write-Host "    Ghost Stop 4.0% VERIFIED" -ForegroundColor Green
        }
    } else {
        Write-Host "    Rust compilation FAILED" -ForegroundColor Red
    }
    Pop-Location
}

# Install Python deps
Write-Host "[10/10] Installing Python dependencies..." -ForegroundColor Yellow
pip install numpy pandas numba scikit-learn pyzmq hmmlearn --quiet 2>&1
Write-Host "    Python dependencies installed" -ForegroundColor Green

# Verify Phase 2.7 Config
Write-Host "`n============================================================================" -ForegroundColor Cyan
Write-Host "    PHASE 2.7 CONFIGURATION VERIFICATION" -ForegroundColor Cyan
Write-Host "============================================================================" -ForegroundColor Cyan

if (Test-Path "$SHF_DIR\src\core\config.py") {
    $config = Get-Content "$SHF_DIR\src\core\config.py" -Raw
    
    if ($config -match "entry_threshold_sigma.*=.*2\.089") { Write-Host "    Entry Z = 2.089 ✓" -ForegroundColor Green } else { Write-Host "    Entry Z MISSING!" -ForegroundColor Red }
    if ($config -match "exit_threshold_sigma.*=.*0\.904") { Write-Host "    Exit Z = 0.904 ✓" -ForegroundColor Green } else { Write-Host "    Exit Z MISSING!" -ForegroundColor Red }
    if ($config -match "stop_threshold_sigma.*=.*4\.815") { Write-Host "    Stop Z = 4.815 ✓" -ForegroundColor Green } else { Write-Host "    Stop Z MISSING!" -ForegroundColor Red }
    if ($config -match "daily_drawdown_ghost_limit.*=.*0\.040") { Write-Host "    Ghost Stop = 4.0% ✓" -ForegroundColor Green } else { Write-Host "    Ghost Stop MISSING!" -ForegroundColor Red }
}

# Download MT5
Write-Host "`n[FINAL] Downloading MetaTrader 5..." -ForegroundColor Yellow
$mt5Url = "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"
Invoke-WebRequest -Uri $mt5Url -OutFile "$SHF_DIR\mt5setup.exe" -UseBasicParsing
Write-Host "    MT5 installer ready at $SHF_DIR\mt5setup.exe" -ForegroundColor Green

Write-Host @"

============================================================================
    DEPLOYMENT COMPLETE - READY FOR 5%ERS LOGIN
============================================================================
    
    NEXT STEPS (Manual):
    
    1. Run MT5 Installer:
       Start-Process "$SHF_DIR\mt5setup.exe"
       
    2. During MT5 Setup:
       - Select broker: The 5%ers
       - Login with your challenge credentials
       
    3. Copy EA to MT5:
       - Find: $SHF_DIR\mt5\SHF_ZMQ_Bridge.mq5
       - Copy to: AppData\Roaming\MetaQuotes\Terminal\<ID>\MQL5\Experts\
       - Press F4 in MT5 to open MetaEditor, compile with F7
       
    4. Enable AutoTrading in MT5:
       - Tools > Options > Expert Advisors
       - [x] Allow DLL imports
       - [x] Allow automated trading
       
    5. Attach EA to DE40 M5 chart
    
    6. Start Python Engine:
       cd $SHF_DIR
       python -m src.engine --dry-run
       
============================================================================
    ENVIRONMENT READY FOR 5%ERS LOGIN
============================================================================
"@ -ForegroundColor Green
