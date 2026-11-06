import subprocess
import tomllib
from pathlib import Path

import pytest

from app import __version__
from app.core import version as version_module
from app.core.version import UNKNOWN_COMMIT, read_commit

BACKEND = Path(__file__).resolve().parents[2]
FULL_SHA = "0123456789abcdef0123456789abcdef01234567"


def test_version_comes_from_pyproject() -> None:
    data = tomllib.loads((BACKEND / "pyproject.toml").read_text(encoding="utf-8"))
    assert __version__ == data["project"]["version"]
    assert __version__.count(".") >= 2


def test_version_file_wins_over_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "VERSION").write_text(f"0.2.0 {FULL_SHA}\n", encoding="utf-8")

    def no_git(*_a: object, **_k: object) -> None:
        raise AssertionError("git must not be consulted when VERSION exists")

    monkeypatch.setattr(subprocess, "run", no_git)
    assert read_commit(tmp_path) == "0123456"


def test_git_is_used_without_version_file(tmp_path: Path) -> None:
    git = ["git", "-C", str(tmp_path)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run(
        [*git, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "--allow-empty",
         "-q", "-m", "x"],
        check=True,
    )  # fmt: skip
    expected = subprocess.run(
        [*git, "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert read_commit(tmp_path) == expected


def test_unknown_when_git_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*_a: object, **_k: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", missing)
    assert read_commit(tmp_path) == UNKNOWN_COMMIT


def test_unknown_when_git_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 128, stdout="", stderr="not a git repository")

    monkeypatch.setattr(subprocess, "run", failing)
    assert read_commit(tmp_path) == UNKNOWN_COMMIT


def test_malformed_version_file_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "VERSION").write_text("only-one-token\n", encoding="utf-8")

    def missing(*_a: object, **_k: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", missing)
    assert read_commit(tmp_path) == UNKNOWN_COMMIT


def test_version_label_format() -> None:
    assert version_module.version_label() == f"v{__version__} · {version_module.get_commit()}"
