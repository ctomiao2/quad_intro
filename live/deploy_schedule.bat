@echo off
rem ============================================================
rem  quad_intro live one-click deploy (double-click to run)
rem  - finds Python, installs deps, bootstraps live data copy,
rem    runs consistency selfcheck, registers scheduled tasks:
rem      quad_intro_live_open  09:31 Mon-Fri
rem      quad_intro_live_close 15:35 Mon-Fri
rem  Re-running is safe (idempotent). Remove: remove_schedule.bat
rem ============================================================
setlocal EnableDelayedExpansion
chcp 65001 >nul
for %%i in ("%~dp0..") do set "ROOT=%%~fi"
cd /d "%ROOT%"
echo Repo root: %ROOT%

echo.
echo [1/6] Locating Python...
set "PY="
python -c "import sys; print(sys.executable)" > "%TEMP%\quad_py_find.txt" 2>nul
if not errorlevel 1 set /p PY=<"%TEMP%\quad_py_find.txt"
if not defined PY (
    py -3 -c "import sys; print(sys.executable)" > "%TEMP%\quad_py_find.txt" 2>nul
    if not errorlevel 1 set /p PY=<"%TEMP%\quad_py_find.txt"
)
if not defined PY (
    echo [ERROR] Python not found. Install Python 3.10+ from https://www.python.org
    echo         Remember to check "Add python.exe to PATH" during install, then re-run.
    goto :fail
)
echo Found: %PY%

echo.
echo [2/6] Installing dependencies (pandas/numpy/matplotlib/requests)...
"%PY%" -m pip install -q -r requirements.txt
if errorlevel 1 (echo [ERROR] pip install failed. Check network/proxy.) else (echo OK)

echo.
echo [3/6] Bootstrap live data copy...
if exist "live\data\etf_daily" (
    echo Already initialized, skip.
) else (
    "%PY%" live.py bootstrap
    if errorlevel 1 goto :fail
)

echo.
echo [4/6] Feishu push config...
if exist "live\state\feishu.json" (
    echo feishu.json found.
) else (
    echo [WARN] live\state\feishu.json NOT found - Feishu push will be skipped.
    echo        To enable: copy live\feishu.example.json -^> live\state\feishu.json
    echo        and fill in your group bot webhook, then run: python live.py notify-test
)

echo.
echo [5/6] Self-check: live vs backtest consistency...
"%PY%" live.py selfcheck
if errorlevel 1 (echo [ERROR] selfcheck FAILED. Do not go live until it passes.) else (echo OK)

echo.
echo [6/6] Registering scheduled tasks...
schtasks /Create /TN "quad_intro_live_open" /TR "\"%PY%\" \"%ROOT%\live.py\" open" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 09:31 /F
if errorlevel 1 (echo [ERROR] schtasks failed, try right-click this .bat -^> Run as administrator) else (echo OK 09:31 open)
schtasks /Create /TN "quad_intro_live_close" /TR "\"%PY%\" \"%ROOT%\live.py\" close" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 15:35 /F
if errorlevel 1 (echo [ERROR] schtasks failed, try right-click this .bat -^> Run as administrator) else (echo OK 15:35 close)

echo.
echo ============================================================
echo  Deploy finished.
echo  - Tasks: quad_intro_live_open 09:31 / quad_intro_live_close 15:35 (Mon-Fri)
echo  - Logs:   live\logs\  (daily orders: live\logs\orders_YYYYMMDD.json)
echo  - Manual: python live.py open ^| close ^| status ^| backfill ^| fill ...
echo  - This PC must be ON at 09:31 / 15:35 on trading days.
echo ============================================================
goto :end

:fail
echo.
echo Deploy FAILED. Fix the error above and re-run.
:end
if /i not "%~1"=="nopause" pause
