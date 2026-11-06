"""CoursePilot backend. ``__version__`` comes from ``[project].version`` in pyproject.toml."""

import tomllib
from importlib import metadata
from pathlib import Path

_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _read_version() -> str:
    """The single source of truth is pyproject.toml (the project is not installed as a package,
    so there is no installed metadata to ask). No network access is involved."""
    try:
        data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
        return str(data["project"]["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        pass
    try:
        return metadata.version("coursepilot-backend")
    except metadata.PackageNotFoundError:
        return "0.0.0+unknown"


__version__ = _read_version()
