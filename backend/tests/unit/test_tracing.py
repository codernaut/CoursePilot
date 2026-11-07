import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.core.tracing import TRACING_FLAGS, apply_tracing_policy
from tests.conftest import make_settings

NAMES = (*TRACING_FLAGS, "LANGSMITH_API_KEY")


@pytest.fixture(autouse=True)
def _env() -> Iterator[None]:
    """The policy writes os.environ directly, so snapshot and restore it around every test."""
    saved = {name: os.environ.get(name) for name in NAMES}
    for name in NAMES:
        os.environ.pop(name, None)
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def test_tracing_is_forced_off_without_a_key_even_when_the_environment_enables_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    assert apply_tracing_policy(make_settings(tmp_path)) is False
    for name in TRACING_FLAGS:
        assert os.environ[name] == "false"


def test_an_empty_or_blank_key_counts_as_not_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    assert apply_tracing_policy(make_settings(tmp_path, langsmith_api_key="   ")) is False
    assert os.environ["LANGSMITH_TRACING"] == "false"


def test_tracing_is_enabled_only_when_the_key_is_set(tmp_path: Path) -> None:
    assert apply_tracing_policy(make_settings(tmp_path, langsmith_api_key="lsv2_test_key")) is True
    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert os.environ["LANGCHAIN_TRACING_V2"] == "true"
    assert os.environ["LANGSMITH_API_KEY"] == "lsv2_test_key"


async def test_create_app_applies_the_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, template_db: Path
) -> None:
    import shutil

    from app.main import create_app

    shutil.copyfile(template_db, tmp_path / "test.db")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    app = create_app(make_settings(tmp_path))
    try:
        assert os.environ["LANGCHAIN_TRACING_V2"] == "false"
    finally:
        await app.state.database.dispose()
