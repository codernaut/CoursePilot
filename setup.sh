#!/usr/bin/env sh
# First-time setup. Finds or installs uv (asking first), makes sure Python 3.12 exists
# (uv can download a private one, asking first), installs dependencies, then runs the
# interactive setup.
set -eu
cd "$(dirname "$0")/backend"

# Which Python (if any) can run "-m pip" / "-m uv"? Not required: uv can bring its own.
PY=""
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
fi

# Step 1: find uv (standalone on PATH, the usual install folders, or "python -m uv").
# Sets UV ("uv" or "python3 -m uv": a command line, deliberately unquoted where used) and returns 1 if none works.
find_uv() {
  if command -v uv >/dev/null 2>&1; then
    UV="uv"
  elif [ -x "$HOME/.local/bin/uv" ]; then
    PATH="$HOME/.local/bin:$PATH" # put the folder on PATH (safe with spaces in $HOME)
    UV="uv"
  elif [ -x "$HOME/.cargo/bin/uv" ]; then
    PATH="$HOME/.cargo/bin:$PATH"
    UV="uv"
  elif [ -n "$PY" ] && "$PY" -m uv --version >/dev/null 2>&1; then
    UV="$PY -m uv"
  else
    UV=""
    return 1
  fi
}

# Asks a yes/no question (default no). Returns 0 only for y / yes.
confirm() {
  printf "%s [y/N] " "$1"
  read -r answer || answer=""
  case "$answer" in
    y | Y | yes | YES) return 0 ;;
    *) return 1 ;;
  esac
}

UV=""
if ! find_uv; then
  # Step 2a: no uv. If a Python exists, try pip first (fails on "externally managed" systems).
  if [ -n "$PY" ]; then
    echo "uv is not installed. Trying: $PY -m pip install --user uv"
    if "$PY" -m pip install --user uv >/dev/null 2>&1 && "$PY" -m uv --version >/dev/null 2>&1; then
      UV="$PY -m uv"
    else
      echo "Could not install uv with pip (this Python may be 'externally managed')." >&2
    fi
  fi

  # Step 2b: offer the official installer. Nothing is downloaded or run without a yes.
  if [ -z "$UV" ]; then
    echo "uv (https://docs.astral.sh/uv/) is required and was not found. It installs the" >&2
    echo "dependencies and can also provide Python 3.12 for CoursePilot." >&2
    echo "The official installer would run:" >&2
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    echo "It installs uv into \$HOME/.local/bin." >&2
    if ! confirm "Run it now?"; then
      echo "Install uv yourself, for example with 'pipx install uv' or from" >&2
      echo "https://docs.astral.sh/uv/getting-started/installation/, then run this script again." >&2
      exit 1
    fi
    if command -v curl >/dev/null 2>&1; then
      curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget >/dev/null 2>&1; then
      wget -qO- https://astral.sh/uv/install.sh | sh
    else
      echo "Neither curl nor wget is available. Install one of them (or 'pipx install uv') and run this script again." >&2
      exit 1
    fi
    # The installer changes the PATH of new shells only: use it in this run, with the full path.
    PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    export PATH
    if ! find_uv; then
      echo "uv was installed but could not be found. Open a new terminal and run this script again." >&2
      exit 1
    fi
  fi
fi

# Step 3: Python 3.12 (pyproject.toml requires >=3.12,<3.13; an existing one is used silently).
# shellcheck disable=SC2086  # $UV is a command line ("python3 -m uv") and must word-split
if ! $UV python find --no-project ">=3.12,<3.13" >/dev/null 2>&1; then
  echo "Python 3.12 was not found on this computer."
  echo "uv can download a private Python 3.12 just for CoursePilot. It does not change or"
  echo "replace any Python you have installed. The command would be: uv python install 3.12"
  if ! confirm "Download Python 3.12 now?"; then
    echo "Install Python 3.12 from https://www.python.org/downloads/ and run this script again." >&2
    exit 1
  fi
  # shellcheck disable=SC2086
  $UV python install 3.12
fi

# Step 4: dependencies, then the interactive setup. --inexact keeps the optional local-embeddings
# extra (PyTorch) if it was installed earlier.
# shellcheck disable=SC2086
$UV sync --inexact
# shellcheck disable=SC2086
$UV run python -m app.cli setup
