# ============================================================================
# DIAG_TODAY.ps1  --  one-line VPS diagnostic for "did the bot TP today?"
# ============================================================================
#
# Run this on the VPS while the bot is still running. Reads the live log
# files, classifies every entry today, and tells you exactly which (if any)
# tickets are stuck.
#
# Usage (on VPS, from the PropBot repo root):
#     .\DIAG_TODAY.ps1                         # today, no MT5 price check
#     .\DIAG_TODAY.ps1 -Tail 90                # last 90 minutes only
#     .\DIAG_TODAY.ps1 -Date 2026-05-05        # specific UTC day
#     .\DIAG_TODAY.ps1 -CheckPrices            # ALSO query MT5 for TP touches
#     .\DIAG_TODAY.ps1 -Save                   # tee output to a file
#
# Output is dumped both to the console and (with -Save) to:
#     Results\diag_<UTC-date>_<UTC-HHMMSS>.txt
#
# Exits 0 on success, 2 on bad arguments. Always safe to run while the bot
# is trading -- read-only on the log files.
# ============================================================================

[CmdletBinding()]
param(
    [string]$Date = "",
    [int]$Tail = 0,
    [string]$Root = "Results",
    [switch]$CheckPrices,
    [switch]$AllTickets,
    [switch]$Save
)

$ErrorActionPreference = "Stop"

# --- locate the script root (so this works whether you cd into the repo or not)
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $repoRoot

# --- pick the right python
$pyExe = $null
foreach ($cand in @("python", "py", "python3")) {
    try {
        $v = & $cand --version 2>&1
        if ($LASTEXITCODE -eq 0) { $pyExe = $cand; break }
    } catch {}
}
if (-not $pyExe) {
    Write-Error "No python interpreter found on PATH (tried: python, py, python3)."
    exit 2
}

# --- assemble args
$pyArgs = @("Scripts\diag_today_signals.py", "--root", $Root)
if ($Tail -gt 0)        { $pyArgs += @("--tail", "$Tail") }
elseif ($Date -ne "")   { $pyArgs += @("--date", $Date) }
if ($CheckPrices)       { $pyArgs += "--check-prices" }
if ($AllTickets)        { $pyArgs += "--all-tickets" }

Write-Host "[DIAG_TODAY] $pyExe $($pyArgs -join ' ')" -ForegroundColor Cyan

if ($Save) {
    $stamp  = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd_HHmmss")
    $outDir = Join-Path $repoRoot "Results"
    if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir | Out-Null }
    $outFile = Join-Path $outDir "diag_$stamp.txt"
    Write-Host "[DIAG_TODAY] tee -> $outFile" -ForegroundColor Cyan
    & $pyExe @pyArgs 2>&1 | Tee-Object -FilePath $outFile
} else {
    & $pyExe @pyArgs
}

exit $LASTEXITCODE
