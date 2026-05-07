# AUDIT_TODAY.ps1 -- run on the VPS
# ---------------------------------
# One-stop forensic audit for "did the bot do the right thing today?"
#
# Pulls latest from git, then runs FOUR diagnostics back-to-back:
#
#   [A] diag_today_full_audit.py
#         per-ticket: every ENTRY today, every subsequent ladder/Layer1/
#         close event, classification incl. STILL_OPEN_OR_NO_EVENT.
#
#   [B] diag_concurrency_cooldown_today.py     <-- NEW (TP-not-firing focus)
#         answers the three questions:
#           1. Did the 2-position cap block any entries?
#           2. Did the 300 s no-chase cooldown block any entries?
#           3. Did 3+ symbols try to fire in the same minute today?
#         + STILL-OPEN tickets: did broker M1 high/low cross TP1 already?
#
#   [C] diag_did_tp1_get_touched.py --days 1
#         the smoking-gun stuck-ladder detector. For every ticket today,
#         pulls broker M1 bars and checks: did the bar high (LONG) or
#         low (SHORT) ever cross TP1, AND did the bot log a TP1_PARTIAL
#         event for that ticket?  YES + NO = STUCK_LADDER_BUG.
#
#   [D] diag_layer1_slippage_today.py
#         Layer1-stop slippage breakdown for the day.
#
# All output is teed to Results\audit_<date>.txt for reference.
#
# Usage on VPS:
#     cd C:\PropBot
#     git pull
#     .\AUDIT_TODAY.ps1                  # today (UTC)
#     .\AUDIT_TODAY.ps1 2026-05-07       # explicit date
#
param(
    [string]$Date = ""
)

$ErrorActionPreference = "Continue"   # don't bail if one diag has no data
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host ""
Write-Host "================================================================================"
Write-Host "  AUDIT_TODAY -- pulling latest, then running full forensic audit"
Write-Host "================================================================================"

# Pull latest scripts (non-fatal if offline)
try {
    git pull --ff-only 2>&1 | Out-Host
} catch {
    Write-Host "[warn] git pull failed -- continuing with local copy"
}

# Resolve date
if ([string]::IsNullOrWhiteSpace($Date)) {
    $Date = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd")
}

$resultsDir = Join-Path $root "Results"
if (-not (Test-Path $resultsDir)) {
    New-Item -ItemType Directory -Force -Path $resultsDir | Out-Null
}
$outFile = Join-Path $resultsDir ("audit_{0}.txt" -f $Date)
# Start fresh so re-runs don't accumulate stale output
if (Test-Path $outFile) { Remove-Item -Force $outFile }

Write-Host ""
Write-Host "  date     : $Date"
Write-Host "  saving to: $outFile"
Write-Host ""

function Run-Diag {
    param(
        [string]$Title,
        [string]$ScriptPath,
        [string[]]$ExtraArgs = @()
    )
    Write-Host ""
    Write-Host "================================================================================"
    Write-Host "  $Title"
    Write-Host "================================================================================"
    Write-Host ""
    # Append a section header to the output file
    "" | Out-File -FilePath $outFile -Encoding utf8 -Append
    "================================================================================" | Out-File -FilePath $outFile -Encoding utf8 -Append
    "  $Title" | Out-File -FilePath $outFile -Encoding utf8 -Append
    "================================================================================" | Out-File -FilePath $outFile -Encoding utf8 -Append
    "" | Out-File -FilePath $outFile -Encoding utf8 -Append
    & python $ScriptPath @ExtraArgs 2>&1 | Tee-Object -FilePath $outFile -Append
}

Run-Diag -Title "[A] PER-TICKET LADDER AUDIT (events.log timeline)" `
         -ScriptPath "Scripts\diag_today_full_audit.py" `
         -ExtraArgs @($Date)

Run-Diag -Title "[B] CONCURRENCY-CAP + 300 s COOLDOWN + STILL-OPEN TP-TOUCH CHECK" `
         -ScriptPath "Scripts\diag_concurrency_cooldown_today.py" `
         -ExtraArgs @($Date)

Run-Diag -Title "[C] STUCK-LADDER DETECTOR  (broker M1 vs bot TP1 events)" `
         -ScriptPath "Scripts\diag_did_tp1_get_touched.py" `
         -ExtraArgs @("--days", "1")

Run-Diag -Title "[D] LAYER1 / SLIPPAGE BREAKDOWN" `
         -ScriptPath "Scripts\diag_layer1_slippage_today.py" `
         -ExtraArgs @($Date)

Write-Host ""
Write-Host "================================================================================"
Write-Host "  Done. Full audit saved to:"
Write-Host "    $outFile"
Write-Host ""
Write-Host "  Quick read:"
Write-Host "    -- Section [A] tells you what happened to each ticket."
Write-Host "    -- Section [B] tells you whether cap+cooldown+3-sym chain hurt today,"
Write-Host "       and for STILL-OPEN tickets whether broker M1 already crossed TP1."
Write-Host "    -- Section [C] is the smoking gun. Look for STUCK_LADDER_BUG counts > 0."
Write-Host "    -- Section [D] shows Layer1 slippage."
Write-Host "================================================================================"
Write-Host ""
