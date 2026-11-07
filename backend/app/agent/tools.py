"""Tools the graph calls. There is deliberately no model-callable tool: the graph drives retrieval,
so the model cannot choose a scope, a user or a query target.

``search_material`` takes the user's access context and the starting scope from ``AgentContext``
(the runtime context set by the server from the session and the conversation). Its only
model-influenced input is the query text, which reaches the vector and FTS5 queries as data.
"""

import asyncio
import time
from dataclasses import dataclass

from app.agent.state import AgentContext
from app.core.logging import get_logger
from app.ingestion.index_meta import ensure_index_matches
from app.retrieval.hybrid import (
    MAX_QUERY_CHARS,
    RetrievedChunk,
    SearchScope,
    hybrid_search,
    is_sufficient,
    tier_scopes,
)
from app.websearch.base import (
    MAX_QUERY_CHARS as WEB_MAX_QUERY_CHARS,
)
from app.websearch.base import (
    WebResult,
    WebSearchError,
    clean_line,
    normalize_allowed,
    sanitize_results,
)

log = get_logger(__name__)

# All web queries of one turn together may take this long; the provider's own retries must not
# eat the turn's AGENT_TIMEOUT_S, because the answer can still be written from the material.
WEB_TOTAL_TIMEOUT_S = 20.0


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


@dataclass(frozen=True)
class WebSearch:
    results: list[WebResult]
    queries: list[str]  # the queries that were sent
    failed: bool  # the provider failed and nothing was found


def web_available(ctx: AgentContext) -> str:
    """``""`` when the web may be searched, else why not: ``disabled`` (the module's toggle is
    off) or ``not_configured`` (no provider or no query budget)."""
    if not ctx.access.may_read_material:  # anonymous or inactive callers get nothing at all
        return "not_configured"
    if not ctx.web_enabled:
        return "disabled"
    if ctx.web is None or ctx.settings.web_max_queries < 1:
        return "not_configured"
    return ""


async def search_web(ctx: AgentContext, *, queries: list[str]) -> WebSearch:
    """Run at most ``WEB_MAX_QUERIES`` of ``queries`` (in order) and stop at the first one that
    returns usable results. Only the query text is sent to the provider. Results are filtered
    again here (http(s) only, ``WEB_ALLOWED_DOMAINS``). A provider failure never raises: it is
    logged and reported as ``failed``."""
    provider = ctx.web
    sent: list[str] = []
    results: list[WebResult] = []
    failed = False
    if provider is None:
        return WebSearch([], sent, False)
    allowed = normalize_allowed(ctx.settings.web_allowed_domains)
    deadline = time.monotonic() + WEB_TOTAL_TIMEOUT_S
    seen: set[str] = set()
    for raw in queries:
        query = clean_line(raw, WEB_MAX_QUERY_CHARS)
        if not query or query in seen:
            continue
        if len(sent) >= ctx.settings.web_max_queries:
            break
        seen.add(query)
        sent.append(query)
        try:
            budget = deadline - time.monotonic()
            if budget <= 0:
                raise TimeoutError
            results = sanitize_results(
                await asyncio.wait_for(provider.search(query), budget), allowed
            )
        except TimeoutError:
            log.warning("web_search_failed", reason="timeout")
            failed = True
            break
        except WebSearchError as exc:
            log.warning("web_search_failed", reason=exc.message)
            failed = True
            continue
        except Exception as exc:  # a provider bug must not break the answer
            log.warning("web_search_failed", reason=type(exc).__name__)
            failed = True
            continue
        failed = False
        if results:
            break
    return WebSearch(results, sent, failed and not results)
