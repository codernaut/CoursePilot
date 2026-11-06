@echo off
rem First-time setup: installs uv if needed, installs dependencies, then runs the interactive setup.
setlocal
cd /d "%~dp0backend"

where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.12 or newer is required but "python" was not found on PATH.
  echo Install it from https://www.python.org/downloads/ and run this script again.
  exit /b 1
)

python -m uv --version >nul 2>nul
if errorlevel 1 (
  echo Installing uv...
  python -m pip install --user uv
  if errorlevel 1 exit /b 1
)

python -m uv sync
if errorlevel 1 exit /b 1

python -m uv run python -m app.cli setup
exit /b %errorlevel%
