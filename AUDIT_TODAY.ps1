# AUDIT_TODAY.ps1 — run on the VPS
# ----------------------------------
# Pulls latest, then prints a per-ticket audit of every entry today,
# every subsequent ladder/Layer1/close event, and a final classification
# (TP1_HIT / SL_HIT / LAYER1_CLOSE / STILL_OPEN / NO_LADDER_EVENTS).
#
# Usage on VPS:
#     cd C:\PropBot
#     .\AUDIT_TODAY.ps1                  # today (UTC)
#     .\AUDIT_TODAY.ps1 2026-05-07       # explicit date
#
# Saves a copy to Results\audit_<date>.txt for reference.

param(
    [string]$Date = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host ""
Write-Host "================================================================================"
Write-Host "  AUDIT_TODAY — pulling latest, then auditing today's entries"
Write-Host "================================================================================"

# Pull latest scripts (non-fatal if offline)
try {
    git pull --ff-only 2>&1 | Out-Host
} catch {
    Write-Host "[warn] git pull failed — continuing with local copy"
}

# Resolve date
if ([string]::IsNullOrWhiteSpace($Date)) {
    $Date = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd")
}

$outFile = Join-Path $root "Results\audit_$Date.txt"
New-Item -ItemType Directory -Force -Path (Split-Path $outFile) | Out-Null

Write-Host ""
Write-Host "  date: $Date"
Write-Host "  saving to: $outFile"
Write-Host ""

# Run the audit, tee to file
python "Scripts\diag_today_full_audit.py" $Date | Tee-Object -FilePath $outFile

Write-Host ""
Write-Host "  Done. Full output saved to: $outFile"
Write-Host ""
