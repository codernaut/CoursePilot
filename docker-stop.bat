@echo off
rem Stop the CoursePilot Docker containers. Your data (the "coursepilot-data" volume) is kept.
rem Never add -v here: "docker compose down -v" deletes the volume with all data.
setlocal
cd /d "%~dp0"
docker compose down
if errorlevel 1 exit /b 1
echo CoursePilot stopped. Data is kept in the Docker volume; start again with docker-start.bat
exit /b 0
