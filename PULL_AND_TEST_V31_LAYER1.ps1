# =============================================================================
#  PULL_AND_TEST_V31_LAYER1.ps1
# =============================================================================
#  Run on the VPS (or your local box) to:
#   1. Pull the latest commit from origin/main
#   2. Show what changed
#   3. Run the 81 Layer 1 unit tests
#   4. Print a single PASS / FAIL line so you know at a glance.
#
#  This script does NOT touch the running bot, does NOT change any live config,
#  does NOT enable Layer 1 in production. It's pure verification.
#
#  Usage (from PowerShell on the VPS):
#       cd C:\PropBot              # or wherever the repo lives
#       .\PULL_AND_TEST_V31_LAYER1.ps1
# =============================================================================

# IMPORTANT: do NOT set ErrorActionPreference = Stop here.  Native commands
# such as `git` write progress info to stderr, and PowerShell would treat it
# as a fatal error.  We check $LASTEXITCODE manually after each call instead.
$ErrorActionPreference = "Continue"

function Write-Step {
    param([string]$Title)
    Write-Host ""
    Write-Host "================================================================" -ForegroundColor Cyan
    Write-Host " $Title" -ForegroundColor Cyan
    Write-Host "================================================================" -ForegroundColor Cyan
}

# -----------------------------------------------------------------------------
# 0. Sanity — make sure we're in a git repo
# -----------------------------------------------------------------------------
Write-Step "0/4  Verifying repository"
if (-not (Test-Path ".git")) {
    Write-Host "ERROR: not a git repo. cd into your PropBot folder first." -ForegroundColor Red
    exit 1
}
$repoPath = (Get-Location).Path
Write-Host "  Repo:    $repoPath"
$currentBranch = (& git rev-parse --abbrev-ref HEAD 2>$null).Trim()
Write-Host "  Branch:  $currentBranch"
$beforeSha = (& git rev-parse --short HEAD 2>$null).Trim()
Write-Host "  Before:  $beforeSha"

# -----------------------------------------------------------------------------
# 1. Fetch & fast-forward
# -----------------------------------------------------------------------------
Write-Step "1/4  Pulling origin/main"

# Redirect git's stderr to stdout (2>&1) so PowerShell stops complaining
# about the "From https://..." progress line.
& git fetch origin main 2>&1 | ForEach-Object { Write-Host "  $_" }
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: git fetch failed (exit $LASTEXITCODE)." -ForegroundColor Red
    exit 2
}

& git pull --ff-only origin main 2>&1 | ForEach-Object { Write-Host "  $_" }
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: git pull failed. Working tree may be dirty." -ForegroundColor Red
    Write-Host "Try:   git status   then   git stash" -ForegroundColor Yellow
    exit 2
}

$afterSha = (& git rev-parse --short HEAD 2>$null).Trim()
Write-Host ""
Write-Host "  After:   $afterSha"

# -----------------------------------------------------------------------------
# 2. Show what landed
# -----------------------------------------------------------------------------
Write-Step "2/4  Changes in this pull"
if ($beforeSha -eq $afterSha) {
    Write-Host "  Already up-to-date - no new commits." -ForegroundColor Yellow
} else {
    & git log --oneline "$beforeSha..$afterSha" 2>&1 | ForEach-Object { Write-Host "  $_" }
    Write-Host ""
    Write-Host "  Files touched:" -ForegroundColor Gray
    & git diff --name-status "$beforeSha..$afterSha" 2>&1 | ForEach-Object { Write-Host "  $_" }
}

# -----------------------------------------------------------------------------
# 3. Confirm new files exist
# -----------------------------------------------------------------------------
Write-Step "3/4  Verifying new Layer 1 files are present"
$expected = @(
    "src/execution/layer1.py",
    "src/execution/layer1_tracker.py",
    "tests/test_layer1.py",
    "tests/test_layer1_tracker.py",
    "Docs/V31_LAYER1_INTEGRATION_GUIDE.md"
)
$missing = @()
foreach ($f in $expected) {
    if (Test-Path $f) {
        Write-Host "  [OK]    $f" -ForegroundColor Green
    } else {
        Write-Host "  [MISS]  $f" -ForegroundColor Red
        $missing += $f
    }
}
if ($missing.Count -gt 0) {
    Write-Host ""
    Write-Host "ERROR: $($missing.Count) expected file(s) missing." -ForegroundColor Red
    exit 3
}

# -----------------------------------------------------------------------------
# 4. Run the 81 unit tests
# -----------------------------------------------------------------------------
Write-Step "4/4  Running 81 Layer 1 unit tests"

# Pick whichever python is on PATH.  On the VPS the system-wide python.exe is
# what the live bot uses, so we deliberately do not require a venv.
& python -m pytest tests/test_layer1.py tests/test_layer1_tracker.py -v --tb=short
$testExit = $LASTEXITCODE

Write-Host ""
Write-Host "================================================================" -ForegroundColor Cyan
if ($testExit -eq 0) {
    Write-Host " RESULT: ALL TESTS PASSED " -ForegroundColor Green -NoNewline
    Write-Host "(commit $afterSha)" -ForegroundColor Gray
    Write-Host "================================================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host " Layer 1 foundation is verified on this machine." -ForegroundColor Green
    Write-Host " Math is locked. No live behaviour has changed yet."
    Write-Host " Live wiring is the next session - see:"
    Write-Host "   Docs\V31_LAYER1_INTEGRATION_GUIDE.md" -ForegroundColor Gray
    exit 0
} else {
    Write-Host " RESULT: TESTS FAILED " -ForegroundColor Red -NoNewline
    Write-Host "(commit $afterSha, exit $testExit)" -ForegroundColor Gray
    Write-Host "================================================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host " Do NOT proceed with deployment. Investigate the pytest output above." -ForegroundColor Red
    exit 4
}
