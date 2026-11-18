@echo off
rem Start CoursePilot in Docker: the web app and the worker from compose.yaml.
rem Optional: start.bat runs CoursePilot without Docker and is unchanged.
rem
rem   docker-start.bat             build the image if it is not there yet, then start
rem   docker-start.bat --rebuild   rebuild the image first (after updating the code)
rem
rem Settings come from .env next to this file (the one setup.bat writes, or a copy of .env.example).
rem Data lives in the Docker volume "coursepilot-data", not in .\data. Stop with docker-stop.bat.
setlocal EnableExtensions
cd /d "%~dp0"
set "IMAGE=coursepilot:local"

where docker >nul 2>&1
if errorlevel 1 (
  echo Docker is not installed. Install Docker Desktop, or use start.bat instead.
  exit /b 1
)
docker info >nul 2>&1
if errorlevel 1 (
  echo Docker is not running. Start Docker Desktop and try again.
  exit /b 1
)
docker compose version >nul 2>&1
if errorlevel 1 (
  echo Docker Compose v2 ^("docker compose"^) is missing. Update Docker Desktop and try again.
  exit /b 1
)

if not exist ".env" (
  copy /y ".env.example" ".env" >nul
  echo Created .env from .env.example.
  echo Add your API keys to .env ^(ANTHROPIC_API_KEY or GOOGLE_API_KEY, and an embedding key^),
  echo or run setup.bat, then run docker-start.bat again.
  exit /b 1
)

set "BUILD="
if /i "%~1"=="--rebuild" set "BUILD=1"
docker image inspect %IMAGE% >nul 2>&1
if errorlevel 1 set "BUILD=1"
if defined BUILD (
  echo Building the CoursePilot image ^(the first build takes a few minutes^)...
  set "GIT_COMMIT="
  for /f "delims=" %%i in ('git rev-parse HEAD 2^>nul') do set "GIT_COMMIT=%%i"
  call docker compose build --build-arg GIT_COMMIT=%%GIT_COMMIT%% web
  if errorlevel 1 exit /b 1
)

echo Starting CoursePilot ^(web app and worker^)...
docker compose up -d --wait --wait-timeout 180
if errorlevel 1 (
  echo CoursePilot did not become ready. See: docker compose logs
  exit /b 1
)

rem Idempotent: creates missing years/blocks/modules, and asks for an administrator only once.
docker compose exec web python -m app.cli seed-structure
if errorlevel 1 exit /b 1
docker compose exec web python -m app.cli create-admin --if-missing
if errorlevel 1 exit /b 1

set "URL=http://localhost:8000"
set "WEB_BIND="
for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
  if "%%a"=="BASE_URL" if not "%%b"=="" set "URL=%%~b"
  if "%%a"=="WEB_BIND" set "WEB_BIND=%%~b"
)

echo.
echo CoursePilot is running at %URL%
echo %URL% | findstr /b /i /c:"http://localhost" /c:"http://127.0.0.1" /c:"https://localhost" >nul
if errorlevel 1 if not defined WEB_BIND (
  echo Note: BASE_URL is not localhost, but the port is published on 127.0.0.1 only.
  echo Set WEB_BIND=0.0.0.0 in .env ^(or put a reverse proxy in front^) and run this again.
)
echo Logs: docker compose logs -f    Stop: docker-stop.bat
start "" "%URL%"
exit /b 0
