#!/usr/bin/env sh
# Start CoursePilot in Docker: the web app and the worker from compose.yaml.
# Optional: ./start.sh runs CoursePilot without Docker and is unchanged.
#
#   ./docker-start.sh             build the image if it is not there yet, then start
#   ./docker-start.sh --rebuild   rebuild the image first (after updating the code)
#
# Settings come from .env next to this file (the one ./setup.sh writes, or a copy of .env.example).
# Data lives in the Docker volume "coursepilot-data", not in ./data. Stop with ./docker-stop.sh.
set -eu
cd "$(dirname "$0")"

IMAGE=coursepilot:local

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is not installed. Install Docker Desktop or Docker Engine, or use ./start.sh instead."
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker is not running. Start Docker Desktop (or the docker service) and try again."
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose v2 ('docker compose') is missing. Update Docker and try again."
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "Created .env from .env.example."
  echo "Add your API keys to .env (ANTHROPIC_API_KEY or GOOGLE_API_KEY, and an embedding key),"
  echo "or run ./setup.sh, then run ./docker-start.sh again."
  exit 1
fi

# The value of KEY in .env (last occurrence, surrounding quotes removed), or the default.
env_value() {
  value=$(sed -n "s/^$1=//p" .env | tail -n 1 | sed 's/^"\(.*\)"$/\1/; s/^'"'"'\(.*\)'"'"'$/\1/')
  if [ -n "$value" ]; then echo "$value"; else echo "$2"; fi
}

if [ "${1:-}" = "--rebuild" ] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "Building the CoursePilot image (the first build takes a few minutes)..."
  GIT_COMMIT=$(git rev-parse HEAD 2>/dev/null || true)
  docker compose build --build-arg GIT_COMMIT="$GIT_COMMIT" web
fi

echo "Starting CoursePilot (web app and worker)..."
docker compose up -d --wait --wait-timeout 180

# Idempotent: creates missing years/blocks/modules, and asks for an administrator only once.
docker compose exec web python -m app.cli seed-structure
docker compose exec web python -m app.cli create-admin --if-missing

URL=$(env_value BASE_URL http://localhost:8000)
echo ""
echo "CoursePilot is running at $URL"
case "$URL" in
  http://localhost*|http://127.0.0.1*|https://localhost*) ;;
  *)
    if [ -z "$(env_value WEB_BIND '')" ]; then
      echo "Note: BASE_URL is not localhost, but the port is published on 127.0.0.1 only."
      echo "Set WEB_BIND=0.0.0.0 in .env (or put a reverse proxy in front) and run this again."
    fi
    ;;
esac
echo "Logs: docker compose logs -f    Stop: ./docker-stop.sh"

if command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$URL" >/dev/null 2>&1 || true
elif command -v open >/dev/null 2>&1; then
  open "$URL" >/dev/null 2>&1 || true
fi
