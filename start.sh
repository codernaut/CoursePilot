#!/usr/bin/env sh
# Runs migrations, starts the web app and the worker, and opens the browser.
set -eu
cd "$(dirname "$0")/backend"

# Same uv discovery as setup.sh: standalone uv (also in its usual install folders),
# else "python -m uv" (uv can also have provided the Python, so none may be on PATH).
PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
export PATH
if command -v uv >/dev/null 2>&1; then
  exec uv run python -m app.cli start
fi
if command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  PY=python
fi
exec "$PY" -m uv run python -m app.cli start
