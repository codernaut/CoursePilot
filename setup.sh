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

# uv can be a standalone "uv" on PATH or the "uv" module of this Python.
UV=""
if command -v uv >/dev/null 2>&1; then
  UV="uv"
elif "$PY" -m uv --version >/dev/null 2>&1; then
  UV="$PY -m uv"
else
  echo "uv is not installed. Trying: $PY -m pip install --user uv"
  # On "externally managed" systems (PEP 668) this fails; we then offer the official installer.
  if "$PY" -m pip install --user uv >/dev/null 2>&1 && "$PY" -m uv --version >/dev/null 2>&1; then
    UV="$PY -m uv"
  else
    echo "Could not install uv with pip (this Python may be 'externally managed')." >&2
    echo "The official installer would run:" >&2
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    printf "Run it now? [y/N] "
    read -r answer || answer=""
    case "$answer" in
      y | Y | yes | YES)
        if ! command -v curl >/dev/null 2>&1; then
          echo "curl is not available. Install uv with 'pipx install uv' and run this script again." >&2
          exit 1
        fi
        curl -LsSf https://astral.sh/uv/install.sh | sh
        PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
        export PATH
        if command -v uv >/dev/null 2>&1; then
          UV="uv"
        else
          echo "uv was installed but is not on PATH yet. Open a new terminal and run this script again." >&2
          exit 1
        fi
        ;;
      *)
        echo "Install uv yourself, for example with 'pipx install uv' (see https://docs.astral.sh/uv/)," >&2
        echo "then run this script again." >&2
        exit 1
        ;;
    esac
  fi
fi

$UV sync
$UV run python -m app.cli setup
