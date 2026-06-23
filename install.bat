@echo off
REM Self-relaunch inside cmd /k so window never auto-closes
if "%~1"=="--run" goto :run
cmd /k "%~dpnx0" --run
exit /b 0

:run
title struct-ai-backtest Installer
echo.
echo ============================================================
echo   struct-ai-backtest - Installer
echo ============================================================
echo.
echo   Window is open and batch file is running correctly.
echo.

REM --- Check Python ---
python --version >NUL 2>&1
if errorlevel 1 (
    echo.
    echo *** ERROR: Python not found. ***
    echo.
    echo   Install Python 3.10 or newer from:
    echo     https://python.org/downloads
    echo   IMPORTANT: tick "Add Python to PATH" during install.
    echo.
    echo Press any key to close...
    pause > nul
    exit /b 1
)
echo [OK] Python found:
python --version
echo.

REM --- Create virtual environment ---
if exist ".venv" (
    echo [OK] .venv already exists, skipping creation.
    goto :install
)

echo Creating virtual environment...
python -m venv .venv
if errorlevel 1 (
    echo.
    echo *** ERROR: Failed to create .venv ***
    echo Try: python -m ensurepip --upgrade
    echo Press any key to close...
    pause > nul
    exit /b 1
)
echo [OK] .venv created.

:install
echo.

REM --- Activate ---
call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo.
    echo *** ERROR: Cannot activate .venv ***
    echo Delete the .venv folder and run install.bat again.
    echo Press any key to close...
    pause > nul
    exit /b 1
)

REM --- Upgrade pip silently ---
python -m pip install --upgrade pip --quiet

REM --- Install packages ---
echo Installing packages from requirements.txt ...
echo This takes 1-3 minutes on first run.
echo.
pip install -r requirements.txt
if errorlevel 1 goto :pkgwarn
goto :pkgdone

:pkgwarn
echo.
echo *** NOTE: One package may have failed to install.
echo     This is usually MetaTrader5 - optional, Windows only.
echo     pandas, numpy and yfinance are required - check those above.
echo.

:pkgdone

echo.
echo ============================================================
echo   Installation complete!
echo ============================================================
echo.
echo NEXT STEPS:
echo   1. Collect market data first (run once, then work offline):
echo.
echo      Option A -- MT5 source (1-year 5M history):
echo        Open a terminal here and type:
echo          start.bat --collect --source mt5
echo        Requires: MetaTrader5 terminal running and logged in.
echo.
echo      Option B -- Yahoo Finance (no MT5 needed, ~60-day 5M):
echo        Open a terminal here and type:
echo          start.bat --collect --source yfinance
echo.
echo   2. After data is collected, double-click start.bat to run.
echo.
echo Press any key to close...
pause > nul
