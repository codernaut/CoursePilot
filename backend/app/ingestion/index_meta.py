"""The embedding invariant (CLAUDE.md section 7.4).

``app_meta`` records the provider, model and dimension ``chunk_vectors`` was built with. Ingestion
(and search from M4) refuse to run when the configured values differ, because vectors from
different models are not comparable and a different dimension cannot even be stored.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError
from app.db.chunk_store import EmbeddingConfig, read_embedding_config

REINDEX_HINT = "An administrator must run 'python -m app.cli reindex-all'."


class IndexMismatchError(AppError):
    """503 ``index_mismatch``: the configured embedding setup differs from the built index."""

    def __init__(self, indexed: EmbeddingConfig | None, configured: EmbeddingConfig) -> None:
        if indexed is None:
            message = (
                "The search index has no recorded embedding settings. "
                f"{REINDEX_HINT} (configured: {configured.describe()})"
            )
        else:
            message = (
                f"The search index was built with {indexed.describe()}, but the server is "
                f"configured for {configured.describe()}. {REINDEX_HINT}"
            )
        super().__init__(
            "index_mismatch",
            message,
            status_code=503,
            details={
                "indexed": None if indexed is None else _as_dict(indexed),
                "configured": _as_dict(configured),
            },
        )


def _as_dict(config: EmbeddingConfig) -> dict[str, object]:
    return {"provider": config.provider, "model": config.model, "dim": config.dim}


def config_from_settings(settings: Settings) -> EmbeddingConfig:
    return EmbeddingConfig(
        settings.embedding_provider, settings.embedding_model, settings.embedding_dim
    )


async def ensure_index_matches(db: AsyncSession, settings: Settings) -> None:
    """Raise ``IndexMismatchError`` unless ``app_meta`` equals the configured embedding setup."""
    configured = config_from_settings(settings)
    indexed = await read_embedding_config(db)
    if indexed != configured:
        raise IndexMismatchError(indexed, configured)
