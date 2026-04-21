# ======================================================================
#  INSTALL_EA.ps1   --   zero-click MT5 EA installer
#
#  What it does:
#    1. Finds every MT5 data folder on the VPS
#    2. Copies  C:\PropBot\MQL5\Experts\SHF_Bridge.mq5  into each one
#    3. Compiles it with metaeditor64.exe (no MetaEditor window needed)
#
#  Usage (from the VPS, normal PowerShell, does NOT need admin):
#      cd C:\PropBot
#      .\INSTALL_EA.ps1
# ======================================================================

$ErrorActionPreference = "Stop"
$SOURCE_MQ5 = "C:\PropBot\MQL5\Experts\SHF_Bridge.mq5"

Write-Host "========================================================"
Write-Host "  SHF_Bridge EA  --  automatic installer"
Write-Host "========================================================`n"

# ---- 0. sanity --------------------------------------------------------
if (-not (Test-Path $SOURCE_MQ5)) {
    throw "Source file missing: $SOURCE_MQ5 (did you run 'git pull'?)"
}

# ---- 1. find all MT5 data folders -------------------------------------
Write-Host "[1/3] Looking for MT5 data folders..."
$roaming = Join-Path $env:APPDATA "MetaQuotes\Terminal"
if (-not (Test-Path $roaming)) {
    throw "No MT5 installation detected under $roaming`n" +
          "Make sure you have installed and launched MT5 at least once."
}

# Each data folder has an MQL5\Experts\ subfolder
$dataFolders = Get-ChildItem -Path $roaming -Directory |
               Where-Object { Test-Path (Join-Path $_.FullName "MQL5\Experts") }

if ($dataFolders.Count -eq 0) {
    throw "Found MT5 root ($roaming) but no data folders with MQL5\Experts yet.`n" +
          "Open MT5 once, then re-run this script."
}

foreach ($d in $dataFolders) {
    Write-Host "       found -> $($d.FullName)"
}

# ---- 2. copy .mq5 into each --------------------------------------------
Write-Host "`n[2/3] Copying SHF_Bridge.mq5 ..."
foreach ($d in $dataFolders) {
    $dst = Join-Path $d.FullName "MQL5\Experts\SHF_Bridge.mq5"
    Copy-Item $SOURCE_MQ5 $dst -Force
    Write-Host "       $dst  -- OK"
}

# ---- 3. locate metaeditor64.exe + compile ------------------------------
Write-Host "`n[3/3] Compiling with metaeditor64.exe ..."
$editors = @()
$candidatesRoot = @(
    "C:\Program Files\MetaTrader 5",
    "C:\Program Files (x86)\MetaTrader 5",
    "${env:ProgramFiles}\The5ers",
    "${env:ProgramFiles(x86)}\The5ers",
    "${env:ProgramFiles}",
    "${env:ProgramFiles(x86)}"
)
foreach ($r in $candidatesRoot) {
    if (Test-Path $r) {
        $editors += Get-ChildItem -Path $r -Recurse -Filter "metaeditor64.exe" `
                    -ErrorAction SilentlyContinue
    }
}
$editors = $editors | Sort-Object FullName -Unique

if ($editors.Count -eq 0) {
    Write-Warning "metaeditor64.exe not found.  The .mq5 file IS copied,"
    Write-Warning "but you'll need to press F7 manually in MT5's MetaEditor"
    Write-Warning "(MT5 will also auto-compile the first time you attach the EA)."
} else {
    $ed = $editors[0].FullName
    Write-Host "       editor -> $ed"
    foreach ($d in $dataFolders) {
        $mq5 = Join-Path $d.FullName "MQL5\Experts\SHF_Bridge.mq5"
        $log = Join-Path $d.FullName "MQL5\Experts\SHF_Bridge.log"
        & "$ed" /compile:"$mq5" /log:"$log" | Out-Null
        $ok = Select-String -Path $log -Pattern "0 errors,\s*\d+\s*warnings" -Quiet
        $ex5 = [IO.Path]::ChangeExtension($mq5, ".ex5")
        if ($ok -or (Test-Path $ex5)) {
            Write-Host "       OK    $ex5"
        } else {
            Write-Host "       FAIL  -- see $log" -ForegroundColor Red
            Get-Content $log | Select-Object -Last 10 | ForEach-Object { "           $_" }
        }
    }
}

Write-Host "`n========================================================"
Write-Host "  ALL DONE"
Write-Host "========================================================"
Write-Host ""
Write-Host "NEXT STEPS (once MT5 is already open and logged in):"
Write-Host ""
Write-Host "  1. In MT5, press Ctrl+N (or go to View -> Navigator)"
Write-Host "  2. Expand Experts -- you will see SHF_Bridge in the list"
Write-Host "     (if not, right-click Experts -> Refresh)"
Write-Host "  3. Double-click SHF_Bridge -> attach to ANY chart"
Write-Host "  4. In the dialog that pops up:"
Write-Host "        Common tab  -> [x] Allow algorithmic trading"
Write-Host "                      -> [x] Allow DLL imports"
Write-Host "        click OK"
Write-Host "  5. Click the big 'AutoTrading' button on MT5 toolbar."
Write-Host "     It turns GREEN when algorithmic trading is enabled."
Write-Host "  6. Check top-right of the chart for a smiley face :-) "
Write-Host "     next to the EA name -- that means the bridge is live."
Write-Host ""
Write-Host "Then run:   python Scripts\smoke_v15_live.py"
Write-Host ""
