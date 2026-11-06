"""Version and git commit shown in the page footer and ``GET /api/v1/health/version``."""

import subprocess
from functools import lru_cache
from pathlib import Path

from app import __version__

UNKNOWN_COMMIT = "unknown"
SHORT_SHA_LEN = 7


def read_commit(repo_root: Path) -> str:
    """Short commit id. A ``VERSION`` file at the repo root (``<version> <full sha>``, written
    into release zips) wins; otherwise ask git. Any failure yields ``unknown``."""
    version_file = repo_root / "VERSION"
    try:
        parts = version_file.read_text(encoding="utf-8").split()
    except OSError:
        parts = []
    if len(parts) >= 2 and parts[1].isalnum():
        return parts[1][:SHORT_SHA_LEN]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607 - git on PATH is expected
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN_COMMIT
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha.isalnum() else UNKNOWN_COMMIT


@lru_cache
def get_commit() -> str:
    from app.core.config import REPO_ROOT

    return read_commit(REPO_ROOT)


def version_label() -> str:
    return f"v{__version__} · {get_commit()}"
