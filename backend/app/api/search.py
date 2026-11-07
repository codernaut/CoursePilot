"""Search and passage endpoints (authenticated; mainly for the search page, debugging and evals)."""

from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.api.deps import CurrentUser, DbDep, SettingsDep
from app.retrieval.access import AccessContext
from app.retrieval.hybrid import SearchFilters
from app.retrieval.query_embedding import QueryEmbedder
from app.schemas.search import Level, PassageOut, SearchHitOut, SearchOut, TierOut
from app.services import search as search_service

router = APIRouter(tags=["search"])


def get_query_embedder(request: Request) -> QueryEmbedder:
    embedder: QueryEmbedder = request.app.state.query_embedder
    return embedder


@router.get("/search", response_model=SearchOut)
async def search(
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    ctx: CurrentUser,
    q: Annotated[str, Query(min_length=1, max_length=2000)],
    module_id: Annotated[str | None, Query(max_length=64)] = None,
    scope: Level | None = None,
    week: Annotated[str | None, Query(max_length=60)] = None,
    document_id: Annotated[str | None, Query(max_length=64)] = None,
    widen: bool = False,
) -> SearchOut:
    """Hybrid search. ``scope`` defaults to ``module`` when ``module_id`` is given, else ``all``.
    ``widen=true`` goes module -> block -> all until a tier is sufficient."""
    search_scope = await search_service.resolve_scope(db, scope=scope, module_id=module_id)
    outcome = await search_service.search(
        db,
        settings=settings,
        embedder=get_query_embedder(request),
        access=AccessContext.from_user(ctx.user),
        query=q,
        scope=search_scope,
        filters=SearchFilters(week=week or None, document_id=document_id or None),
        widen=widen,
    )
    return SearchOut(
        query=outcome.query,
        tiers=[
            TierOut(
                level=t.scope.level,
                target_id=t.scope.target_id,
                hit_count=len(t.chunks),
                sufficient=t.sufficient,
                best_vector_similarity=t.best_vector_similarity,
            )
            for t in outcome.tiers
        ],
        hits=[SearchHitOut.from_chunk(c) for c in outcome.chunks],
    )


@router.get("/chunks/{chunk_id}", response_model=PassageOut)
async def get_chunk(chunk_id: str, db: DbDep, ctx: CurrentUser) -> PassageOut:
    chunk = await search_service.read_passage(
        db, access=AccessContext.from_user(ctx.user), chunk_id=chunk_id
    )
    return PassageOut.from_chunk(chunk)
