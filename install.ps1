# struct-ai-backtest  PowerShell Installer
  # If install.bat won't open, right-click this file and choose
  # "Run with PowerShell" instead.

  Write-Host ""
  Write-Host "============================================"  -ForegroundColor Cyan
  Write-Host "  struct-ai-backtest Installer"               -ForegroundColor Cyan
  Write-Host "============================================"  -ForegroundColor Cyan
  Write-Host ""

  $ErrorActionPreference = "Stop"
  Set-Location $PSScriptRoot

  # Check Python
  try {
      $v = & python --version 2>&1
      Write-Host "[OK] $v" -ForegroundColor Green
  } catch {
      Write-Host "ERROR: Python not found. Install from https://python.org" -ForegroundColor Red
      Read-Host "Press ENTER to exit"
      exit 1
  }

  # Create venv
  if (-not (Test-Path ".venv")) {
      Write-Host "Creating .venv..." -ForegroundColor Yellow
      & python -m venv .venv
      Write-Host "[OK] .venv created." -ForegroundColor Green
  } else {
      Write-Host "[OK] .venv exists." -ForegroundColor Green
  }

  # Activate and install
  Write-Host ""
  Write-Host "Installing packages..." -ForegroundColor Yellow
  & .venv\Scripts\python.exe -m pip install --upgrade pip --quiet
  & .venv\Scripts\pip.exe install -r requirements.txt

  Write-Host ""
  Write-Host "============================================" -ForegroundColor Green
  Write-Host "  Installation complete!"                    -ForegroundColor Green
  Write-Host "============================================" -ForegroundColor Green
  Write-Host ""
  Write-Host "Next: double-click start.bat to run."
  Read-Host "Press ENTER to close"
  