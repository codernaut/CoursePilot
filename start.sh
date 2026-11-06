#!/usr/bin/env sh
# Runs migrations, starts the web app and the worker, and opens the browser.
set -eu
cd "$(dirname "$0")/backend"

if command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  PY=python
fi

exec "$PY" -m uv run python -m app.cli start
