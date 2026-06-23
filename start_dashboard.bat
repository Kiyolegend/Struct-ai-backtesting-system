@echo off
REM ============================================================
REM  struct-ai-backtest -- Web Dashboard Launcher
REM  Opens a browser UI at http://localhost:5050
REM ============================================================

if "%~1"=="--run" goto :run
cmd /k "%~dpnx0" --run
exit /b 0

:run
title struct-ai-backtest Dashboard

if not exist ".venv\Scripts\activate.bat" (
    echo.
    echo *** ERROR: .venv not found ***
    echo Run install.bat first.
    echo.
    pause > nul
    exit /b 1
)

call .venv\Scripts\activate.bat

echo.
echo ============================================================
echo   STRUCT.ai Backtest Dashboard
echo   Opening http://localhost:5050 in your browser...
echo   Press Ctrl+C to stop the server.
echo ============================================================
echo.

start "" "http://localhost:5050"
python dashboard\app.py

echo.
echo Dashboard stopped.
pause > nul
