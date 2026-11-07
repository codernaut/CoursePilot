"""Application settings. This is the ONLY module that reads environment variables."""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# .env and relative data paths live at the repository root, whatever the working directory.
REPO_ROOT = Path(__file__).resolve().parents[3]
INSECURE_SECRET_KEYS = {"", "change-me", "changeme", "secret"}
MIN_PRODUCTION_SECRET_KEY_LENGTH = 32
_SQLITE_PREFIX = "sqlite+aiosqlite:///"


def _anchor(path: str) -> str:
    p = Path(path)
    return str(p if p.is_absolute() else (REPO_ROOT / p).resolve())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # App
    app_env: Literal["development", "test", "production"] = "development"
    app_host: str = "127.0.0.1"
    app_port: int = Field(8000, ge=1, le=65535)
    base_url: str = "http://localhost:8000"
    secret_key: SecretStr = SecretStr("change-me")
    session_ttl_hours: int = Field(12, ge=1)
    login_rate_limit_per_min: int = Field(10, ge=1)
    behind_proxy: bool = False
    # For a closed, trusted LAN without TLS only: lets production run on an http:// BASE_URL
    # (non-Secure "session" cookie, no HSTS). Everything else stays on.
    allow_insecure_http: bool = False
    max_request_body_mb: int = Field(2, ge=1)

    # Data
    data_dir: str = "./data"
    database_url: str = "sqlite+aiosqlite:///./data/coursepilot.db"
    checkpoint_db_path: str = "./data/checkpoints.db"

    # File storage
    storage_backend: Literal["local", "s3"] = "local"
    s3_endpoint_url: str | None = None
    s3_bucket: str = "coursepilot-materials"
    s3_access_key_id: SecretStr | None = None
    s3_secret_access_key: SecretStr | None = None
    s3_region: str = "auto"

    # Programme structure
    program_years: int = Field(3, ge=1)
    blocks_per_year: int = Field(3, ge=1)
    modules_per_block: int = Field(3, ge=1)

    # LLM ("fake" is for tests only)
    llm_provider: Literal["anthropic", "gemini", "fake"] = "anthropic"
    llm_model: str = "claude-sonnet-5-5"
    llm_fast_model: str = "claude-haiku-5-5"
    anthropic_api_key: SecretStr | None = None
    google_api_key: SecretStr | None = None

    # Embeddings ("fake" is for tests only)
    embedding_provider: Literal["gemini", "voyage", "openai", "fake"] = "gemini"
    embedding_model: str = "gemini-embedding-001"
    embedding_dim: int = Field(768, ge=1)
    voyage_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None

    # Web search
    web_search_provider: Literal["tavily", "none"] = "tavily"
    tavily_api_key: SecretStr | None = None
    web_max_queries: int = Field(2, ge=0)
    web_allowed_domains: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # Retrieval
    retrieval_top_k: int = Field(8, ge=1)
    retrieval_min_score: float = Field(0.0, ge=0.0, le=1.0)  # min cosine similarity
    retrieval_min_hits: int = Field(1, ge=0)
    rerank_enabled: bool = False

    # Limits
    upload_max_mb: int = Field(50, ge=1)
    ocr_enabled: bool = False
    chat_daily_limit_per_user: int = Field(100, ge=1)
    agent_timeout_s: int = Field(60, ge=1)

    # Processing worker
    worker_poll_interval_s: float = Field(2.0, gt=0)
    job_lock_timeout_s: int = Field(1800, ge=30)  # a running job older than this is re-queued
    heartbeat_stale_s: int = Field(120, ge=15)  # no heartbeat for this long = the worker died
    parse_max_pages: int = Field(2000, ge=1)  # pages (PDF) or slides (PPTX) per document
    parse_timeout_s: int = Field(120, ge=1)  # per-document parsing time limit
    embedding_batch_size: int = Field(64, ge=1, le=100)

    # Retention
    audit_retention_days: int = Field(365, ge=1)
    login_failed_audit_retention_days: int = Field(30, ge=1)

    # Observability
    langsmith_api_key: SecretStr | None = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @field_validator("web_allowed_domains", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator(
        "s3_endpoint_url",
        "s3_access_key_id",
        "s3_secret_access_key",
        "anthropic_api_key",
        "google_api_key",
        "voyage_api_key",
        "openai_api_key",
        "tavily_api_key",
        "langsmith_api_key",
        mode="before",
    )
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _check_and_resolve(self) -> Self:
        if self.app_env == "production":
            key = self.secret_key.get_secret_value()
            if key.strip().lower() in INSECURE_SECRET_KEYS:
                raise ValueError("SECRET_KEY must be set to a random value in production")
            if len(key) < MIN_PRODUCTION_SECRET_KEY_LENGTH:
                raise ValueError(
                    f"SECRET_KEY must be at least {MIN_PRODUCTION_SECRET_KEY_LENGTH} characters"
                    " in production"
                )
            if self.allow_insecure_http:
                if not self.base_url.startswith(("http://", "https://")):
                    raise ValueError("BASE_URL must start with http:// or https://")
            elif not self.base_url.startswith("https://"):
                raise ValueError(
                    "BASE_URL must be https:// in production (Secure cookies); "
                    "set ALLOW_INSECURE_HTTP=true only for a closed trusted network"
                )
        # Relative paths are anchored at the repo root so every process sees the same files.
        self.data_dir = _anchor(self.data_dir)
        self.checkpoint_db_path = _anchor(self.checkpoint_db_path)
        if self.database_url.startswith(_SQLITE_PREFIX):
            path = self.database_url[len(_SQLITE_PREFIX) :]
            if path and not path.startswith(":memory:"):
                self.database_url = _SQLITE_PREFIX + _anchor(path)
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def insecure_http_active(self) -> bool:
        """Production deliberately served over plain http://. The flag does nothing for an
        https:// BASE_URL, so a forgotten flag there cannot weaken cookies or drop HSTS."""
        return (
            self.is_production
            and self.allow_insecure_http
            and not self.base_url.startswith("https://")
        )

    @property
    def secure_cookies(self) -> bool:
        """``__Host-session`` + Secure cookie and HSTS: production unless explicitly insecure."""
        return self.is_production and not self.insecure_http_active

    @property
    def max_request_body_bytes(self) -> int:
        return self.max_request_body_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
