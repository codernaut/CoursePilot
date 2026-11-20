@echo off
rem First-time setup. Finds or installs uv (asking first), makes sure Python 3.12 exists
rem (uv can download a private one, asking first), installs dependencies, then runs the
rem interactive setup. No Python is needed before uv exists.
setlocal EnableExtensions
cd /d "%~dp0backend"

rem ---- Step 1: find uv (standalone on PATH, the usual install folder, or "python -m uv") ----
set "UV="
call :find_uv
if defined UV goto have_uv

rem ---- Step 2a: no uv. If a Python exists, try pip first (quiet, no prompt) ----
where python >nul 2>nul
if errorlevel 1 goto ask_installer
echo uv is not installed. Running: python -m pip install --user uv
python -m pip install --user uv
python -m uv --version >nul 2>nul
if errorlevel 1 goto ask_installer
set "UV=python -m uv"
goto have_uv

rem ---- Step 2b: pip did not work or there is no Python: offer the official installer ----
:ask_installer
echo.
echo uv (https://docs.astral.sh/uv/) is required and was not found. It installs the
echo dependencies and can also provide Python 3.12 for CoursePilot.
echo The official installer would run:
echo   powershell -NoProfile -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
echo It installs uv into %USERPROFILE%\.local\bin and adds that folder to your user PATH.
set "ANSWER="
set /p "ANSWER=Run it now? [y/N] "
if /i "%ANSWER%"=="y" goto run_installer
if /i "%ANSWER%"=="yes" goto run_installer
echo.
echo Install uv yourself, for example with: winget install --id=astral-sh.uv -e
echo or see https://docs.astral.sh/uv/getting-started/installation/ and run this script again.
exit /b 1

:run_installer
powershell -NoProfile -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
if errorlevel 1 (
  echo The uv installer failed. See https://docs.astral.sh/uv/getting-started/installation/
  exit /b 1
)
rem The installer changes the PATH of new terminals only: use it in this run, with the full path.
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
set "UV="
call :find_uv
if defined UV goto have_uv
echo uv was installed but could not be found. Open a new terminal and run this script again.
exit /b 1

rem ---- Step 3: Python 3.12 (pyproject.toml pins 3.12.x; an existing one is used silently) ----
:have_uv
%UV% python find --no-project ">=3.12,<3.13" >nul 2>nul
if not errorlevel 1 goto have_python
echo.
echo Python 3.12 was not found on this computer.
echo uv can download a private Python 3.12 just for CoursePilot. It does not change or
echo replace any Python you have installed. The command would be: uv python install 3.12
set "ANSWER="
set /p "ANSWER=Download Python 3.12 now? [y/N] "
if /i "%ANSWER%"=="y" goto install_python
if /i "%ANSWER%"=="yes" goto install_python
echo.
echo Install Python 3.12 from https://www.python.org/downloads/ and run this script again.
exit /b 1

:install_python
%UV% python install 3.12
if errorlevel 1 (
  echo Could not install Python 3.12 with uv. Install it from https://www.python.org/downloads/
  exit /b 1
)

rem ---- Step 4: dependencies, then the interactive setup ----
:have_python
rem --inexact keeps the optional local-embeddings extra (PyTorch) if it was installed earlier.
%UV% sync --inexact
if errorlevel 1 exit /b 1

%UV% run python -m app.cli setup
exit /b %errorlevel%

rem Sets UV to the first working uv: "uv" on PATH, the install folders, or "python -m uv".
:find_uv
uv --version >nul 2>nul
if not errorlevel 1 goto found_path
if exist "%USERPROFILE%\.local\bin\uv.exe" goto found_local
if exist "%USERPROFILE%\.cargo\bin\uv.exe" goto found_cargo
python -m uv --version >nul 2>nul
if not errorlevel 1 goto found_module
exit /b 1
:found_path
set "UV=uv"
exit /b 0
:found_local
set UV="%USERPROFILE%\.local\bin\uv.exe"
exit /b 0
:found_cargo
set UV="%USERPROFILE%\.cargo\bin\uv.exe"
exit /b 0
:found_module
set "UV=python -m uv"
exit /b 0
