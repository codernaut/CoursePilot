#!/usr/bin/env sh
# Stop the CoursePilot Docker containers. Your data (the "coursepilot-data" volume) is kept.
# Never add -v here: `docker compose down -v` deletes the volume with all data.
set -eu
cd "$(dirname "$0")"
docker compose down
echo "CoursePilot stopped. Data is kept in the Docker volume; start again with ./docker-start.sh"
