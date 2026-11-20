@echo off
rem Runs migrations, starts the web app and the worker, and opens the browser.
rem Same uv discovery as setup.bat: standalone uv (PATH or the install folder), else "python -m uv".
setlocal EnableExtensions
cd /d "%~dp0backend"
set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
uv --version >nul 2>nul
if not errorlevel 1 goto have_uv
python -m uv run python -m app.cli start
exit /b %errorlevel%
:have_uv
uv run python -m app.cli start
exit /b %errorlevel%
