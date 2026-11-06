#!/usr/bin/env sh
# First-time setup: installs uv if needed, installs dependencies, then runs the interactive setup.
set -eu
cd "$(dirname "$0")/backend"

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "Python 3.12 or newer is required but was not found on PATH." >&2
  exit 1
fi

if ! "$PY" -m uv --version >/dev/null 2>&1; then
  echo "Installing uv..."
  "$PY" -m pip install --user uv
fi

"$PY" -m uv sync
"$PY" -m uv run python -m app.cli setup
