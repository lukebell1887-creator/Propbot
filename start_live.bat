@echo off
REM =====================================================================
REM  PropBot v15 — VPS watchdog launcher
REM  - Activates venv
REM  - Runs live engine in infinite restart loop (auto-recover on crash)
REM  - Logs every session to logs\live_YYYY-MM-DD.log
REM =====================================================================

setlocal EnableDelayedExpansion

REM --- config: edit these two lines if you want a different risk-scale --
set RISK_SCALE=0.5
set PYTHON_MAIN=Scripts\run_v15_live.py
REM ----------------------------------------------------------------------

cd /d "%~dp0"
if not exist logs mkdir logs

echo.
echo =====================================================================
echo  PropBot v15 live runner starting on %COMPUTERNAME% at %DATE% %TIME%
echo  Risk scale: %RISK_SCALE%   (0.5 = half size; 1.0 = full)
echo  Python   : %PYTHON_MAIN%
echo =====================================================================

:LOOP
REM --- Activate venv
call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo [ERROR] Failed to activate venv. Rebuilding...
    python -m venv .venv
    call .venv\Scripts\activate.bat
    pip install -r requirements.txt
)

REM --- timestamped log
for /f "tokens=2 delims==" %%a in ('wmic os get LocalDateTime /value') do set "dt=%%a"
set "STAMP=%dt:~0,4%-%dt:~4,2%-%dt:~6,2%"
set "LOG=logs\live_%STAMP%.log"

echo.
echo [%DATE% %TIME%] Starting engine (--live --risk-scale %RISK_SCALE%) ... 1>> "%LOG%"
echo [%DATE% %TIME%] Starting engine (--live --risk-scale %RISK_SCALE%) ...

python "%PYTHON_MAIN%" --live --risk-scale %RISK_SCALE% 1>> "%LOG%" 2>&1

echo [%DATE% %TIME%] Engine exited with code %ERRORLEVEL%.  Restarting in 30 s...  1>> "%LOG%"
echo [%DATE% %TIME%] Engine exited (code %ERRORLEVEL%). Restarting in 30 s...

timeout /t 30 /nobreak >nul
goto LOOP
