# =====================================================================
# BOOTSTRAP_VPS.ps1  —  one-shot VPS setup for SHF v13 SmartBB
# =====================================================================
# Usage:
#   1. RDP into the Contabo Windows VPS (IP 158.220.91.19 etc)
#   2. Open PowerShell AS ADMINISTRATOR (right-click the Start menu -> PowerShell (Admin))
#   3. Paste:
#        Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#        irm https://raw.githubusercontent.com/lukebell1887-creator/PropBot/main/BOOTSTRAP_VPS.ps1 | iex
#      (or copy this file to C:\ and run: powershell -ExecutionPolicy Bypass -File C:\BOOTSTRAP_VPS.ps1)
#
# What it does:
#   - Installs Chocolatey, Git, Python 3.11
#   - Clones the PropBot repo
#   - Sets up a Python venv + installs requirements
#   - Prints next-steps (MT5 install and EA compile — those are manual)
# =====================================================================

$ErrorActionPreference = "Stop"

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  PropBot SHF v13 — VPS Bootstrap" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# --- 1. Require admin -------------------------------------------------
if (-not ([Security.Principal.WindowsPrincipal] `
          [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
          [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "ERROR: Run this in an ELEVATED PowerShell (Run as Administrator)" -ForegroundColor Red
    exit 1
}

# --- 2. Install Chocolatey (package manager) --------------------------
if (-not (Get-Command choco -ErrorAction SilentlyContinue)) {
    Write-Host "`n[1/6] Installing Chocolatey..." -ForegroundColor Yellow
    Set-ExecutionPolicy Bypass -Scope Process -Force
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072
    iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
} else {
    Write-Host "`n[1/6] Chocolatey already installed." -ForegroundColor Green
}

# --- 3. Install tools --------------------------------------------------
Write-Host "`n[2/6] Installing Git + Python 3.11..." -ForegroundColor Yellow
choco install -y git python311 --no-progress
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")

# --- 4. Clone repo ----------------------------------------------------
$repoDir = "C:\PropBot"
if (-not (Test-Path $repoDir)) {
    Write-Host "`n[3/6] Cloning PropBot repo..." -ForegroundColor Yellow
    git clone https://github.com/lukebell1887-creator/PropBot.git $repoDir
} else {
    Write-Host "`n[3/6] Repo exists — pulling latest..." -ForegroundColor Green
    Push-Location $repoDir
    git pull --rebase
    Pop-Location
}

# --- 5. Python venv + deps --------------------------------------------
Write-Host "`n[4/6] Setting up Python venv..." -ForegroundColor Yellow
Push-Location $repoDir
if (-not (Test-Path "$repoDir\.venv")) {
    python -m venv .venv
}
& "$repoDir\.venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
& "$repoDir\.venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet
Pop-Location

# --- 6. Firewall rule for bridge port 5555 (loopback-only, MT5 -> Python)
Write-Host "`n[5/6] Adding inbound firewall allow for TCP 5555 (loopback only)..." -ForegroundColor Yellow
New-NetFirewallRule -DisplayName "PropBot Bridge 5555" `
                    -Direction Inbound -Action Allow `
                    -Protocol TCP -LocalPort 5555 `
                    -LocalAddress 127.0.0.1 `
                    -ErrorAction SilentlyContinue | Out-Null

# --- 7. Create start_live.bat -----------------------------------------
Write-Host "`n[6/6] Creating start_live.bat..." -ForegroundColor Yellow
$startBat = @"
@echo off
cd /d C:\PropBot
call .venv\Scripts\activate.bat
REM 0.3% risk, high-quality-only Z>=3.3, auto-restart on crash
:loop
python Scripts\run_live_smartbb.py --risk 0.003 --z-min 3.3 --host 127.0.0.1 >> Results\live_smartbb.log 2>&1
echo Crashed — restart in 30s >> Results\live_smartbb.log
timeout /t 30 /nobreak >nul
goto loop
"@
$startBat | Out-File -FilePath "C:\PropBot\start_live.bat" -Encoding ASCII

# --- Done -------------------------------------------------------------
Write-Host "`n============================================================" -ForegroundColor Green
Write-Host "  BOOTSTRAP COMPLETE" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "NEXT (manual) STEPS:" -ForegroundColor Cyan
Write-Host ""
Write-Host "  1. Install MT5 from your 5%ers dashboard:" -ForegroundColor White
Write-Host "     https://www.the5ers.com/  ->  Client Area  ->  Download MT5" -ForegroundColor Gray
Write-Host ""
Write-Host "  2. Log in to MT5 using 5%ers credentials (server/login/password)." -ForegroundColor White
Write-Host ""
Write-Host "  3. Install the EA:" -ForegroundColor White
Write-Host "     In MT5: File -> Open Data Folder -> MQL5\Experts" -ForegroundColor Gray
Write-Host "     Copy: C:\PropBot\MQL5\Experts\SHF_Bridge.mq5 into that folder" -ForegroundColor Gray
Write-Host "     Back in MT5, press F4 to open MetaEditor, open SHF_Bridge.mq5, press F7 to compile." -ForegroundColor Gray
Write-Host ""
Write-Host "  4. In MT5:" -ForegroundColor White
Write-Host "     - Tools -> Options -> Expert Advisors:" -ForegroundColor Gray
Write-Host "         [x] Allow algorithmic trading" -ForegroundColor Gray
Write-Host "     - Right-click Market Watch -> Show All" -ForegroundColor Gray
Write-Host "       Ensure you can see: US100, US500, US30, DE40, USOIL" -ForegroundColor Gray
Write-Host "       (broker may use variants: NAS100, SPX500, DJ30, DAX40, WTI)" -ForegroundColor Gray
Write-Host "     - Attach SHF_Bridge.mq5 to any chart (e.g. US100 M1)" -ForegroundColor Gray
Write-Host "     - Click AutoTrading button (top toolbar) — turns GREEN" -ForegroundColor Gray
Write-Host ""
Write-Host "  5. DRY-RUN first (24 hours, no real orders):" -ForegroundColor White
Write-Host "     cd C:\PropBot" -ForegroundColor Gray
Write-Host "     .\.venv\Scripts\Activate.ps1" -ForegroundColor Gray
Write-Host "     python Scripts\run_live_smartbb.py --dry-run" -ForegroundColor Gray
Write-Host ""
Write-Host "  6. GO LIVE:" -ForegroundColor White
Write-Host "     .\start_live.bat" -ForegroundColor Gray
Write-Host ""
Write-Host "  7. Configure autostart via Task Scheduler (see Docs\GO_LIVE_SMARTBB_v13.md §2.4)" -ForegroundColor White
Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
