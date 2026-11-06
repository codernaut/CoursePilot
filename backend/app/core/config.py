"""Application settings. This is the ONLY module that reads environment variables."""

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    app_env: Literal["development", "test", "production"] = "development"
    api_base_url: str = "http://localhost:8000"
    web_base_url: str = "http://localhost:3000"
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )
    jwt_secret: SecretStr = SecretStr("change-me")
    access_token_ttl_min: int = 30
    refresh_token_ttl_days: int = 14

    # Database
    database_url: str = "postgresql+asyncpg://coursepilot:coursepilot@localhost:5432/coursepilot"

    # Object storage
    s3_endpoint_url: str | None = "http://localhost:9000"
    s3_bucket: str = "coursepilot-materials"
    s3_access_key_id: SecretStr = SecretStr("minioadmin")
    s3_secret_access_key: SecretStr = SecretStr("minioadmin")
    s3_region: str = "auto"

    # LLM
    llm_provider: Literal["anthropic", "openai", "groq", "ollama"] = "anthropic"
    llm_model: str = ""
    llm_fast_model: str = ""
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    groq_api_key: SecretStr | None = None
    ollama_base_url: str = "http://localhost:11434"

    # Embeddings
    embedding_provider: Literal["openai", "local"] = "openai"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536

    # Web search
    web_search_provider: Literal["tavily", "brave", "none"] = "tavily"
    tavily_api_key: SecretStr | None = None
    brave_api_key: SecretStr | None = None
    web_max_queries: int = 2

    # Retrieval
    retrieval_top_k: int = 8
    retrieval_min_score: float = 0.0
    retrieval_min_hits: int = 1
    rerank_enabled: bool = False

    # Limits
    upload_max_mb: int = 50
    ocr_enabled: bool = False
    chat_daily_limit_per_user: int = 100
    agent_timeout_s: int = 60

    # Observability
    langsmith_api_key: SecretStr | None = None
    log_level: str = "INFO"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator(
        "anthropic_api_key",
        "openai_api_key",
        "groq_api_key",
        "tavily_api_key",
        "brave_api_key",
        "langsmith_api_key",
        mode="before",
    )
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        return None if value == "" else value

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
