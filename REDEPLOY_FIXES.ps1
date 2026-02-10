# ============================================================
# SHF v5.6 — REDEPLOY WITH BUG FIXES
# ============================================================
# Run this on your LOCAL PC (not VPS)
# 
# What it does:
#   1. Pushes fixed Python files to VPS via git
#   2. Tells you exactly what to do on VPS
#
# FIXES INCLUDED:
#   - EA: res.deal -> res.order (positions now close properly)
#   - Bridge: auto-close orphan on spread leg failure  
#   - Engine: cooldown after rejection (no retry spam)
# ============================================================

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  SHF v5.6 REDEPLOY WITH BUG FIXES"
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Step 1: Git commit and push all fixes
Write-Host "[1/2] Committing and pushing fixes..." -ForegroundColor Yellow
git add -A
git commit -m "FIX: ticket mismatch (res.deal->res.order), orphan auto-close, rejection cooldown"
git push origin main

Write-Host ""
Write-Host "[2/2] DONE pushing to GitHub." -ForegroundColor Green
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  NOW DO THESE STEPS ON YOUR VPS:"
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  STEP 1: Open PowerShell on VPS" -ForegroundColor White
Write-Host ""
Write-Host "  STEP 2: Pull the latest code:" -ForegroundColor White
Write-Host "    cd C:\PropBot" -ForegroundColor Green
Write-Host "    git pull origin main" -ForegroundColor Green
Write-Host ""
Write-Host "  STEP 3: Copy the FIXED EA to MT5:" -ForegroundColor White
Write-Host "    Copy-Item 'C:\PropBot\MQL5\Experts\SHF_Bridge.mq5' 'C:\Users\Administrator\AppData\Roaming\MetaQuotes\Terminal\*\MQL5\Experts\' -Force" -ForegroundColor Green
Write-Host ""
Write-Host "  STEP 4: Open MetaEditor in MT5:" -ForegroundColor White
Write-Host "    - Press F4 in MT5 (opens MetaEditor)" -ForegroundColor Yellow
Write-Host "    - Open SHF_Bridge.mq5" -ForegroundColor Yellow
Write-Host "    - Press F7 (Compile)" -ForegroundColor Yellow
Write-Host "    - Check: 0 errors in output" -ForegroundColor Yellow
Write-Host "    - Close MetaEditor" -ForegroundColor Yellow
Write-Host ""
Write-Host "  STEP 5: Reload EA on chart:" -ForegroundColor White
Write-Host "    - Right-click EA on chart -> Remove Expert" -ForegroundColor Yellow
Write-Host "    - Drag SHF_Bridge back onto chart" -ForegroundColor Yellow
Write-Host "    - Tick 'Allow DLL imports' and 'Allow automated trading'" -ForegroundColor Yellow
Write-Host "    - Click OK" -ForegroundColor Yellow
Write-Host ""
Write-Host "  STEP 6: Start the bot:" -ForegroundColor White
Write-Host "    cd C:\PropBot" -ForegroundColor Green
Write-Host "    python -m src.engine" -ForegroundColor Green
Write-Host ""
Write-Host "  STEP 7: Watch for first trade — verify:" -ForegroundColor White
Write-Host "    - 'Order executed: ticket=XXXXXX' (ticket should be large number)" -ForegroundColor Yellow
Write-Host "    - EXIT should show 'Position XXXXXX closed' (NOT 'not found')" -ForegroundColor Yellow
Write-Host "    - If any SPREAD LEG IMBALANCE: should see 'ORPHAN CLOSED'" -ForegroundColor Yellow
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  DONE. BOT IS LIVE WITH FIXES." 
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
