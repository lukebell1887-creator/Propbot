# ============================================================================
# Install mql-zmq library into MT5 for SHF_ZMQ_Bridge EA
# ============================================================================
# Paste this into PowerShell on the VPS
# ============================================================================

$ErrorActionPreference = "Continue"

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Installing ZMQ Library for MT5" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan

# Step 1: Download mql-zmq from GitHub
Write-Host "`n[1/4] Downloading mql-zmq library..." -ForegroundColor Yellow
$zipUrl = "https://github.com/dingmaotu/mql-zmq/archive/refs/heads/master.zip"
$zipPath = "$env:TEMP\mql-zmq.zip"
$extractPath = "$env:TEMP\mql-zmq-extract"

# Clean up any previous attempt
if (Test-Path $extractPath) { Remove-Item -Recurse -Force $extractPath }

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -UseBasicParsing
Write-Host "  Downloaded to $zipPath" -ForegroundColor Green

# Step 2: Extract
Write-Host "[2/4] Extracting..." -ForegroundColor Yellow
Expand-Archive -Path $zipPath -DestinationPath $extractPath -Force
$zmqRoot = "$extractPath\mql-zmq-master"
Write-Host "  Extracted to $zmqRoot" -ForegroundColor Green

# Step 3: Find all MT5 terminal data folders
Write-Host "[3/4] Finding MT5 data folders..." -ForegroundColor Yellow
$mt5Base = "$env:APPDATA\MetaQuotes\Terminal"
$installed = 0

if (Test-Path $mt5Base) {
    $terminals = Get-ChildItem -Path $mt5Base -Directory
    foreach ($terminal in $terminals) {
        $mql5Dir = "$($terminal.FullName)\MQL5"
        if (Test-Path $mql5Dir) {
            Write-Host "  Found: $mql5Dir" -ForegroundColor Gray
            
            # Copy Include/Zmq folder
            $includeDir = "$mql5Dir\Include"
            if (-Not (Test-Path $includeDir)) {
                New-Item -ItemType Directory -Path $includeDir -Force | Out-Null
            }
            
            # Copy the Zmq include folder
            if (Test-Path "$zmqRoot\Include\Zmq") {
                Copy-Item -Recurse -Force "$zmqRoot\Include\Zmq" "$includeDir\Zmq"
                Write-Host "    Copied Include/Zmq/" -ForegroundColor Green
            }
            
            # Also copy any other include files (Mql/ folder if exists)
            if (Test-Path "$zmqRoot\Include\Mql") {
                Copy-Item -Recurse -Force "$zmqRoot\Include\Mql" "$includeDir\Mql"
                Write-Host "    Copied Include/Mql/" -ForegroundColor Green
            }
            
            # Copy Libraries (DLLs)
            $libDir = "$mql5Dir\Libraries"
            if (-Not (Test-Path $libDir)) {
                New-Item -ItemType Directory -Path $libDir -Force | Out-Null
            }
            
            # Copy all DLL files from the library
            $dllSource = "$zmqRoot\Library\MT5"
            if (Test-Path $dllSource) {
                Copy-Item -Force "$dllSource\*" "$libDir\"
                Write-Host "    Copied Libraries/ (DLLs from Library/MT5)" -ForegroundColor Green
            }
            
            # Also check for DLLs in Libraries folder directly
            if (Test-Path "$zmqRoot\Libraries") {
                Get-ChildItem "$zmqRoot\Libraries\*" -Include "*.dll","*.ex5" -ErrorAction SilentlyContinue | ForEach-Object {
                    Copy-Item -Force $_.FullName "$libDir\"
                    Write-Host "    Copied $($_.Name) to Libraries/" -ForegroundColor Green
                }
            }
            
            $installed++
        }
    }
}

# Step 4: Verify
Write-Host "`n[4/4] Verifying installation..." -ForegroundColor Yellow

if ($installed -eq 0) {
    Write-Host "  ERROR: No MT5 terminal folders found!" -ForegroundColor Red
    exit 1
}

# Check the first terminal
$firstTerminal = (Get-ChildItem -Path $mt5Base -Directory | Select-Object -First 1).FullName
$zmqCheck = "$firstTerminal\MQL5\Include\Zmq"
if (Test-Path $zmqCheck) {
    $files = Get-ChildItem -Path $zmqCheck -Recurse
    Write-Host "  Zmq Include folder: $($files.Count) files" -ForegroundColor Green
    
    # List key files
    if (Test-Path "$zmqCheck\Zmq.mqh") {
        Write-Host "  Zmq.mqh: FOUND" -ForegroundColor Green
    } else {
        # Try to find it
        $zmqMqh = Get-ChildItem -Path "$firstTerminal\MQL5\Include" -Recurse -Filter "Zmq.mqh" -ErrorAction SilentlyContinue
        if ($zmqMqh) {
            Write-Host "  Zmq.mqh found at: $($zmqMqh.FullName)" -ForegroundColor Yellow
        } else {
            Write-Host "  WARNING: Zmq.mqh not found in expected location" -ForegroundColor Yellow
        }
    }
} else {
    Write-Host "  WARNING: Zmq folder not found at expected path" -ForegroundColor Yellow
}

# Show what was downloaded
Write-Host "`n  Downloaded library structure:" -ForegroundColor Gray
Get-ChildItem -Path $zmqRoot -Recurse -Depth 2 | ForEach-Object {
    $indent = "    " + ("  " * ($_.FullName.Split('\').Count - $zmqRoot.Split('\').Count))
    if ($_.PSIsContainer) {
        Write-Host "${indent}$($_.Name)/" -ForegroundColor Cyan
    } else {
        Write-Host "${indent}$($_.Name) ($([Math]::Round($_.Length/1024,1))KB)" -ForegroundColor Gray
    }
}

Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host "  ZMQ Library installed to $installed MT5 terminal(s)" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host ""
Write-Host "  NEXT: Go to MetaEditor (F4 in MT5)" -ForegroundColor White
Write-Host "  Open SHF_ZMQ_Bridge.mq5 and press F7 to compile" -ForegroundColor White
Write-Host ""
