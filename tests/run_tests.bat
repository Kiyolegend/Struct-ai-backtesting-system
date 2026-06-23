@echo off
REM ============================================================
REM  struct-ai-backtest -- Test Runner
REM ============================================================

if "%~1"=="--run" goto :run
cmd /k "%~dpnx0" --run
exit /b 0

:run
title struct-ai-backtest Tests

if not exist "..\venv\Scripts\activate.bat" (
    if not exist "..\\.venv\Scripts\activate.bat" (
        echo ERROR: .venv not found. Run install.bat first.
        pause > nul
        exit /b 1
    )
)

cd /d "%~dp0.."
call .venv\Scripts\activate.bat

echo.
echo ============================================================
echo   Running struct-ai-backtest test suite...
echo ============================================================
echo.

python -m pytest tests\test_backtest.py -v --tb=short 2>nul
if errorlevel 1 (
    python tests\test_backtest.py
)

echo.
echo ============================================================
echo   Tests complete.
echo ============================================================
echo.
pause > nul
