# SETUP_VPS.ps1 - one-shot first-time setup on a fresh VPS.
# Installs all Python dependencies and runs the 24-test safety gate.
# Idempotent: safe to re-run after `git pull`.
#
# Usage:   .\SETUP_VPS.ps1

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

Write-Host "================================================================" -ForegroundColor Cyan
Write-Host "  PropBot VPS setup - first-time or post-pull" -ForegroundColor Cyan
Write-Host "================================================================" -ForegroundColor Cyan

# 1. Python present?
$py = (Get-Command python -ErrorAction SilentlyContinue).Path
if (-not $py) { Write-Error "python not on PATH - install Python 3.11+ first"; exit 1 }
Write-Host "[OK] python found at $py" -ForegroundColor Green

# 2. Upgrade pip
Write-Host "`n[1/4] Upgrading pip ..." -ForegroundColor Yellow
& python -m pip install --upgrade pip

# 3. Install requirements
Write-Host "`n[2/4] Installing requirements.txt ..." -ForegroundColor Yellow
& python -m pip install -r "$PSScriptRoot\requirements.txt"
if ($LASTEXITCODE -ne 0) { Write-Error "pip install failed"; exit 3 }

# 4. Verify core imports
Write-Host "`n[3/4] Verifying core imports ..." -ForegroundColor Yellow
& python -c "import numpy, pandas, scipy, sklearn, hmmlearn, pytest; print('  numpy    ', numpy.__version__); print('  pandas   ', pandas.__version__); print('  scipy    ', scipy.__version__); print('  sklearn  ', sklearn.__version__); print('  hmmlearn ', hmmlearn.__version__); print('  pytest   ', pytest.__version__)"
if ($LASTEXITCODE -ne 0) { Write-Error "core import failed"; exit 4 }

# 5. Run the 24-test safety gate
Write-Host "`n[4/4] Running 24-test safety gate ..." -ForegroundColor Yellow
& python -m pytest "$PSScriptRoot\tests\test_live_backtest_parity.py" "$PSScriptRoot\tests\test_dd_breaker.py" "$PSScriptRoot\tests\test_daily_halt.py" -v
if ($LASTEXITCODE -ne 0) { Write-Error "SAFETY TESTS FAILED - do not deploy"; exit 5 }

Write-Host "`n================================================================" -ForegroundColor Green
Write-Host "  [DONE] VPS is ready. Next steps:" -ForegroundColor Green
Write-Host "    1. Attach SHF_Bridge.mq5 EA to any MT5 chart" -ForegroundColor Green
Write-Host "    2. Run .\GO_DRYRUN_V23.ps1 (watch 2+ hours)" -ForegroundColor Green
Write-Host "    3. When happy: .\GO_LIVE_V23.ps1" -ForegroundColor Green
Write-Host "================================================================" -ForegroundColor Green
