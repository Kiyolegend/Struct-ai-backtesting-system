@echo off
REM ============================================================
REM  struct-ai-backtest -- Launcher
REM ============================================================
REM  Double-click = run all 10 strategies on USD/JPY
REM
REM  FIRST RUN: collect market data first:
REM    start.bat --collect
REM
REM  Data collection options:
REM    start.bat --collect                            auto-detect source (full)
REM    start.bat --collect --source mt5               MT5 full (1-year 5M history)
REM    start.bat --collect --source yfinance          Yahoo Finance full (~60-day 5M)
REM    start.bat --collect --refresh                  fast top-up (new bars only)
REM    start.bat --collect --refresh --source mt5     top-up via MT5
REM    start.bat --collect --refresh --source yfinance  top-up via Yahoo
REM
REM  MT5 symbol helpers (run these if collect shows 0 bars):
REM    start.bat --collect --detect-symbols           scan broker, show best symbol match per pair
REM    start.bat --collect --list-symbols             list ALL symbols your broker offers
REM    start.bat --collect --list-symbols --filter JPY   filter to JPY symbols only
REM
REM  Other examples:
REM    start.bat --symbol EUR/USD
REM    start.bat --strategies SC1 SC2 SW1
REM    start.bat --strategies SW1 SW2 SW3 SW4
REM    start.bat --lot 0.01 --debug
REM ============================================================

REM Self-relaunch inside "cmd /k" so window never auto-closes
if "%~1"=="--run" goto :run
if "%~1"=="" goto :selflaunch
REM If extra args given, pass them through properly
:passthrough
cmd /k "%~dpnx0" --run %*
exit /b 0
:selflaunch
cmd /k "%~dpnx0" --run
exit /b 0

:run
REM Strip the --run sentinel from argument list
shift
title struct-ai-backtest

REM --- Check venv ---
if not exist ".venv\Scripts\activate.bat" (
    echo.
    echo *** ERROR: .venv not found ***
    echo Run install.bat first.
    echo.
    echo Press any key to close...
    pause > nul
    exit /b 1
)

REM --- Activate ---
call .venv\Scripts\activate.bat

REM --- Run ---
echo.
python run_backtest.py %1 %2 %3 %4 %5 %6 %7 %8 %9
echo.
echo ============================================================
echo   Backtest finished. Results saved to the results\ folder.
echo ============================================================
echo.
echo Press any key to close...
pause > nul
