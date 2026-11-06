@echo off
rem Runs migrations, starts the web app and the worker, and opens the browser.
setlocal
cd /d "%~dp0backend"
python -m uv run python -m app.cli start
exit /b %errorlevel%
