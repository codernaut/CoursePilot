"""Search business logic: validation, the embedding invariant, the query embedding and retrieval.

Used by the JSON API and the search page, so both enforce the same rules.
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError, not_found, provider_not_configured, validation_error
from app.core.logging import get_logger
from app.ingestion.embedder import EmbeddingError, EmbeddingNotConfiguredError
from app.ingestion.index_meta import ensure_index_matches
from app.models import Block, Module
from app.retrieval.access import AccessContext
from app.retrieval.hybrid import (
    MAX_QUERY_CHARS,
    Level,
    RetrievedChunk,
    SearchFilters,
    SearchScope,
    TierResult,
    get_passage,
    hybrid_search,
    is_sufficient,
    search_tiered,
)
from app.retrieval.query_embedding import QueryEmbedder

log = get_logger(__name__)

SCOPE_LEVELS: tuple[Level, ...] = ("module", "block", "all")


@dataclass(frozen=True)
class SearchOutcome:
    """What a search returned. ``tiers`` lists every tier searched (a single one unless
    ``widen`` was requested); ``chunks`` are the hits of the last tier searched."""

    query: str
    tiers: list[TierResult]

    @property
    def chunks(self) -> list[RetrievedChunk]:
        return self.tiers[-1].chunks if self.tiers else []


def clean_query(query: str) -> str:
    cleaned = " ".join("".join(c if c.isprintable() else " " for c in query).split())
    if not cleaned:
        raise validation_error("The search query must not be empty.")
    if len(cleaned) > MAX_QUERY_CHARS:
        raise validation_error(
            f"The search query is too long (at most {MAX_QUERY_CHARS} characters)."
        )
    return cleaned


async def resolve_scope(
    db: AsyncSession, *, scope: str | None, module_id: str | None
) -> SearchScope:
    """``scope`` defaults to ``module`` when a module is given and to ``all`` otherwise. A module
    or block scope needs ``module_id`` (the block is the module's block)."""
    level = scope or ("module" if module_id else "all")
    if level not in SCOPE_LEVELS:
        raise validation_error("scope must be one of: module, block, all.")
    module: Module | None = None
    if module_id:
        module = await db.get(Module, module_id)
        if module is None:
            raise not_found("Module not found.")
    if level == "all":
        return SearchScope("all")
    if module is None:
        raise validation_error(f"scope '{level}' needs a module_id.")
    if level == "module":
        return SearchScope("module", module.id)
    block = await db.get(Block, module.block_id)
    if block is None:  # pragma: no cover - the foreign key guarantees it
        raise not_found("Module not found.")
    return SearchScope("block", block.id)


async def prepare_query_vector(
    db: AsyncSession, settings: Settings, embedder: QueryEmbedder, query: str
) -> list[float]:
    """The embedding invariant first (503 ``index_mismatch``), then the query vector. There is no
    silent keyword-only fallback: without a working embedding provider the search is refused
    (503 ``provider_not_configured``) so results never quietly change quality."""
    await ensure_index_matches(db, settings)
    try:
        return await embedder.embed(query)
    except EmbeddingNotConfiguredError as exc:
        raise provider_not_configured(exc.message) from exc
    except EmbeddingError as exc:
        # The provider's message is for faculty/admins (logs), not for every signed-in user.
        log.warning("query_embedding_failed", reason=exc.message, retryable=exc.retryable)
        raise AppError(
            "embedding_unavailable",
            "The search service is temporarily unavailable. Please try again later.",
            status_code=502,
        ) from exc


async def search(
    db: AsyncSession,
    *,
    settings: Settings,
    embedder: QueryEmbedder,
    access: AccessContext,
    query: str,
    scope: SearchScope,
    filters: SearchFilters | None = None,
    widen: bool = False,
    top_k: int | None = None,
) -> SearchOutcome:
    query = clean_query(query)
    vector = await prepare_query_vector(db, settings, embedder, query)
    k = top_k or settings.retrieval_top_k
    if widen:
        tiers = await search_tiered(
            db,
            access=access,
            start=scope,
            query=query,
            query_vector=vector,
            filters=filters,
            top_k=k,
            min_score=settings.retrieval_min_score,
            min_hits=settings.retrieval_min_hits,
        )
        return SearchOutcome(query, tiers)
    chunks = await hybrid_search(
        db,
        access=access,
        scope=scope,
        query=query,
        query_vector=vector,
        filters=filters,
        top_k=k,
    )
    sufficient = is_sufficient(
        chunks, min_score=settings.retrieval_min_score, min_hits=settings.retrieval_min_hits
    )
    return SearchOutcome(query, [TierResult(scope, chunks, sufficient)])


async def read_passage(db: AsyncSession, *, access: AccessContext, chunk_id: str) -> RetrievedChunk:
    chunk = await get_passage(db, access=access, chunk_id=chunk_id)
    if chunk is None:
        raise not_found("Passage not found.")
    return chunk


def document_link(chunk: RetrievedChunk) -> str:
    """The document download URL, with ``#page=N`` when the passage has a page."""
    base = f"/api/v1/documents/{chunk.document_id}/download"
    return f"{base}#page={chunk.page_start}" if chunk.page_start is not None else base


def snippet(text: str, limit: int = 400) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit].rstrip() + "…"
