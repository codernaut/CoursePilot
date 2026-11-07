"""Tools the graph calls. There is deliberately no model-callable tool: the graph drives retrieval,
so the model cannot choose a scope, a user or a query target.

``search_material`` takes the user's access context and the starting scope from ``AgentContext``
(the runtime context set by the server from the session and the conversation). Its only
model-influenced input is the query text, which reaches the vector and FTS5 queries as data.
"""

from dataclasses import dataclass

from app.agent.state import AgentContext
from app.ingestion.index_meta import ensure_index_matches
from app.retrieval.hybrid import (
    MAX_QUERY_CHARS,
    RetrievedChunk,
    SearchScope,
    hybrid_search,
    is_sufficient,
    tier_scopes,
)


@dataclass(frozen=True)
class MaterialSearch:
    scope: SearchScope
    chunks: list[RetrievedChunk]
    sufficient: bool  # by similarity threshold and minimum hits
    has_wider: bool  # a wider tier (module -> block -> all) exists after this one


async def search_material(ctx: AgentContext, *, query: str, tier_index: int) -> MaterialSearch:
    """Search tier ``tier_index`` (0 = the starting scope, then its block, then all material).

    No database transaction is open while the query is embedded. Raises ``IndexMismatchError``
    (503) when the embedding setup differs from the index, and the embedder's own errors.
    """
    query = " ".join(query.split())[:MAX_QUERY_CHARS]
    async with ctx.database.read_session() as db:
        await ensure_index_matches(db, ctx.settings)
        scopes = await tier_scopes(db, ctx.scope)
    index = max(0, min(tier_index, len(scopes) - 1))
    vector = await ctx.embedder.embed(query)  # may call the embedding provider
    async with ctx.database.read_session() as db:
        chunks = await hybrid_search(
            db,
            access=ctx.access,
            scope=scopes[index],
            query=query,
            query_vector=vector,
            top_k=ctx.settings.retrieval_top_k,
        )
    return MaterialSearch(
        scope=scopes[index],
        chunks=chunks,
        sufficient=is_sufficient(
            chunks,
            min_score=ctx.settings.retrieval_min_score,
            min_hits=ctx.settings.retrieval_min_hits,
        ),
        has_wider=index + 1 < len(scopes),
    )
