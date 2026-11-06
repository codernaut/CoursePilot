from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import REPO_ROOT, Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[3] / ".env.example"
GOOD_KEY = "k" * 40


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must not depend on the developer's environment."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def _production(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("BASE_URL", "https://example.edu")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


def test_web_allowed_domains_parsed_from_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_ALLOWED_DOMAINS", "example.edu, docs.example.org ,")
    settings = Settings(_env_file=None)
    assert settings.web_allowed_domains == ["example.edu", "docs.example.org"]


def test_web_allowed_domains_empty_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_ALLOWED_DOMAINS", "")
    settings = Settings(_env_file=None)
    assert settings.web_allowed_domains == []


@pytest.mark.parametrize(
    "name",
    ["ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "VOYAGE_API_KEY", "OPENAI_API_KEY", "TAVILY_API_KEY"],
)
def test_empty_api_key_becomes_none(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv(name, "")
    settings = Settings(_env_file=None)
    assert getattr(settings, name.lower()) is None


def test_secrets_are_not_exposed_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "super-secret-value")
    settings = Settings(_env_file=None)
    assert "super-secret-value" not in repr(settings)
    assert settings.google_api_key is not None
    assert settings.google_api_key.get_secret_value() == "super-secret-value"


def test_defaults_match_design() -> None:
    settings = Settings(_env_file=None)
    assert settings.llm_provider == "anthropic"
    assert settings.llm_model == "claude-sonnet-5-5"
    assert settings.llm_fast_model == "claude-haiku-5-5"
    assert settings.embedding_provider == "gemini"
    assert settings.embedding_model == "gemini-embedding-001"
    assert settings.embedding_dim == 768
    assert (settings.program_years, settings.blocks_per_year, settings.modules_per_block) == (
        3,
        3,
        3,
    )
    assert settings.storage_backend == "local"
    assert settings.database_url.startswith("sqlite+aiosqlite:///")


def test_invalid_provider_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    with pytest.raises(ValueError, match="llm_provider"):
        Settings(_env_file=None)


def test_env_example_matches_settings() -> None:
    """Every variable in .env.example exists in Settings, and vice versa (CLAUDE.md section 17)."""
    keys = {
        line.split("=", 1)[0].strip().lower()
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
    }
    assert keys == set(Settings.model_fields)


@pytest.mark.parametrize("key", ["change-me", "", "Change-Me ", "short-key"])
def test_production_rejects_weak_secret_key(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        _production(monkeypatch, SECRET_KEY=key)


def test_production_rejects_default_secret_key_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        _production(monkeypatch)


def test_production_accepts_strong_secret_key(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _production(monkeypatch, SECRET_KEY=GOOD_KEY)
    assert settings.is_production


def test_production_requires_https_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="BASE_URL"):
        _production(monkeypatch, SECRET_KEY=GOOD_KEY, BASE_URL="http://example.edu")


def test_development_accepts_default_secret_key() -> None:
    assert Settings(_env_file=None).secret_key.get_secret_value() == "change-me"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("APP_PORT", "0"),
        ("APP_PORT", "70000"),
        ("SESSION_TTL_HOURS", "0"),
        ("LOGIN_RATE_LIMIT_PER_MIN", "0"),
        ("PROGRAM_YEARS", "0"),
        ("BLOCKS_PER_YEAR", "-1"),
        ("MODULES_PER_BLOCK", "0"),
        ("EMBEDDING_DIM", "0"),
        ("RETRIEVAL_TOP_K", "0"),
        ("RETRIEVAL_MIN_HITS", "-1"),
        ("UPLOAD_MAX_MB", "0"),
        ("CHAT_DAILY_LIMIT_PER_USER", "0"),
        ("AGENT_TIMEOUT_S", "0"),
        ("WEB_MAX_QUERIES", "-1"),
        ("LOG_LEVEL", "LOUD"),
    ],
)
def test_out_of_range_values_are_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError, match=name.lower()):
        Settings(_env_file=None)


def test_log_level_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "debug")
    assert Settings(_env_file=None).log_level == "DEBUG"


def test_relative_paths_are_anchored_at_repo_root() -> None:
    settings = Settings(_env_file=None)
    assert Path(settings.data_dir) == (REPO_ROOT / "data").resolve()
    assert Path(settings.checkpoint_db_path) == (REPO_ROOT / "data" / "checkpoints.db").resolve()
    prefix = "sqlite+aiosqlite:///"
    assert Path(settings.database_url.removeprefix(prefix)).is_absolute()


def test_absolute_paths_are_kept(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    settings = Settings(_env_file=None)
    assert settings.data_dir == str(tmp_path)
    assert settings.database_url == f"sqlite+aiosqlite:///{db}"


def test_env_example_loads_and_equals_defaults() -> None:
    """The documented example values must be valid and must not drift from the code defaults."""
    from_example = Settings(_env_file=ENV_EXAMPLE)
    assert from_example.model_dump() == Settings(_env_file=None).model_dump()


def test_allow_insecure_http_defaults_to_false() -> None:
    assert Settings(_env_file=None).allow_insecure_http is False


def test_production_accepts_http_base_url_only_with_allow_insecure_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _production(
        monkeypatch,
        SECRET_KEY=GOOD_KEY,
        BASE_URL="http://192.168.1.20:8000",
        ALLOW_INSECURE_HTTP="true",
    )
    assert settings.is_production and settings.allow_insecure_http
    assert settings.secure_cookies is False


def test_production_still_rejects_http_base_url_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="ALLOW_INSECURE_HTTP"):
        _production(monkeypatch, SECRET_KEY=GOOD_KEY, BASE_URL="http://192.168.1.20:8000")


def test_allow_insecure_http_still_requires_a_url_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="BASE_URL"):
        _production(
            monkeypatch, SECRET_KEY=GOOD_KEY, BASE_URL="192.168.1.20", ALLOW_INSECURE_HTTP="true"
        )


def test_allow_insecure_http_keeps_the_secret_key_check(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        _production(
            monkeypatch,
            SECRET_KEY="short-key",
            BASE_URL="http://192.168.1.20:8000",
            ALLOW_INSECURE_HTTP="true",
        )


def test_https_production_is_secure_even_if_flag_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _production(monkeypatch, SECRET_KEY=GOOD_KEY, ALLOW_INSECURE_HTTP="true")
    assert settings.allow_insecure_http is True
    assert settings.secure_cookies is True
    assert settings.insecure_http_active is False


def test_http_production_with_flag_is_insecure(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _production(
        monkeypatch,
        SECRET_KEY=GOOD_KEY,
        BASE_URL="http://192.168.1.20:8000",
        ALLOW_INSECURE_HTTP="true",
    )
    assert settings.secure_cookies is False
    assert settings.insecure_http_active is True


def test_retention_and_body_limit_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.audit_retention_days == 365
    assert settings.login_failed_audit_retention_days == 30
    assert settings.max_request_body_mb == 2
    assert settings.max_request_body_bytes == 2 * 1024 * 1024


@pytest.mark.parametrize(
    "name", ["AUDIT_RETENTION_DAYS", "LOGIN_FAILED_AUDIT_RETENTION_DAYS", "MAX_REQUEST_BODY_MB"]
)
def test_new_limits_must_be_positive(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv(name, "0")
    with pytest.raises(ValidationError, match=name.lower()):
        Settings(_env_file=None)
