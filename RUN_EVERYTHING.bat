@echo off
REM ======================================================================
REM  RUN_EVERYTHING.bat  —  Run all PhD-grade scenario tests end-to-end.
REM ======================================================================
REM  Total runtime: ~50 min on a decent CPU
REM
REM  TESTS PERFORMED:
REM    1. SmartBB v18 honest 3-way reference  (CONTROL / REV_PROPER / +NSB)
REM    2. ORB v20 PhD grid search  (648 cells, walk-forward IS+OOS+FULL,
REM                                 Deflated Sharpe, PBO)
REM    3. Aggregates both into ONE paste-ready summary
REM
REM  OUTPUT for you to paste back to Cline:
REM    Results\PASTE_BACK_TO_CLINE.txt
REM ======================================================================

cd /d "%~dp0"

echo =====================================================================
echo  STEP 1/3 : SmartBB v18 honest 3-way reference test  (~1 min)
echo =====================================================================
python -u Scripts\backtest_v19_honest.py > Results\_honest_ref.txt 2>&1
if errorlevel 1 (
    echo   WARNING: SmartBB honest test returned non-zero. See Results\_honest_ref.txt
)
echo   Done. Output saved to Results\_honest_ref.txt
echo.

echo =====================================================================
echo  STEP 2/3 : ORB v20 PhD grid search  (~45 min, be patient)
echo =====================================================================
echo   Progress will be visible in Results\phd_superior_v20_progress.txt
echo   (updates every cell)
echo.
python -u Scripts\phd_superior_v20.py > Results\_phd_superior_v20.txt 2>&1
if errorlevel 1 (
    echo   WARNING: ORB grid returned non-zero. See Results\_phd_superior_v20.txt
)
echo   Done. Full JSON saved to Results\phd_superior_v20.json
echo.

echo =====================================================================
echo  STEP 3/3 : Aggregate all results into one paste-back summary
echo =====================================================================
python -u Scripts\summarize_all_tests.py > Results\PASTE_BACK_TO_CLINE.txt 2>&1
echo.

echo =====================================================================
echo  ALL DONE!
echo =====================================================================
echo.
echo   Open this file and copy its contents:
echo     Results\PASTE_BACK_TO_CLINE.txt
echo.
echo   Then paste it into Cline.  That's it.
echo.
pause
