"""Hybrid retrieval (CLAUDE.md section 8): sqlite-vec cosine top-k + FTS5 bm25 top-k, fused with
Reciprocal Rank Fusion, then the top ``RETRIEVAL_TOP_K`` chunks.

* Scope (module | block | all), document status ``ready``, the access predicate and the optional
  ``week`` / ``document_id`` filters are all in the ``WHERE`` clause of BOTH queries. Nothing is
  filtered after the fact in Python, so a global top-k can never crowd out in-scope rows and an
  out-of-scope row is never produced.
* The vec0 KNN query cannot join, so it filters with ``rowid IN (SELECT seq FROM chunks JOIN
  documents ... WHERE <predicate>)``; the FTS query joins ``chunks``/``documents`` directly.
* User text never reaches ``MATCH`` syntax: ``build_fts_query`` keeps only word tokens and
  double-quotes each one.
* ``search_tiered`` implements module -> block -> all widening; whether to continue after the last
  tier (the web) and the LLM's own sufficiency judgement belong to the agent (M5).
* The ``score`` of a hit is its RRF score (rank based, at most about 2/61) and only orders results.
  ``vector_similarity`` (1 - cosine distance, ``None`` for keyword-only hits) drives sufficiency.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import sqlite_vec
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.retrieval.access import AccessContext, access_filter

Level = Literal["module", "block", "all"]
LEVELS: tuple[Level, ...] = ("module", "block", "all")

RRF_K = 60
CANDIDATES = 20  # top-k of each of the two rankings before fusion
LLM_TEXT_CHARS = 1000  # default of RETRIEVAL_LLM_CHARS
MAX_QUERY_CHARS = 1000
MAX_FTS_TOKENS = 32
MAX_TOKEN_CHARS = 64
PATH_SEPARATOR = " › "  # noqa: RUF001 - the Year > Block > Module separator

_WORD = re.compile(r"\w+", re.UNICODE)


@dataclass(frozen=True)
class SearchScope:
    """``target_id`` is the module id (level ``module``), the block id (``block``) or ``None``."""

    level: Level
    target_id: str | None = None

    def __post_init__(self) -> None:
        if (self.level == "all") != (self.target_id is None):
            raise ValueError("a module or block scope needs a target id; 'all' has none")


@dataclass(frozen=True)
class SearchFilters:
    week: str | None = None
    document_id: str | None = None


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: str
    document_id: str
    document_title: str
    document_filename: str
    module_id: str
    block_id: str
    year_title: str
    block_title: str
    module_title: str
    page_start: int | None
    page_end: int | None
    slide_number: int | None
    section_path: str | None
    text: str  # the full raw passage (for the passage viewer)
    score: float  # fused RRF score
    tier: Level
    week: str | None = None
    ordinal: int = 0
    token_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    vector_similarity: float | None = None

    @property
    def path(self) -> str:
        return PATH_SEPARATOR.join((self.year_title, self.block_title, self.module_title))

    @property
    def llm_text(self) -> str:
        """The text sent to the LLM with the default budget (the agent passes
        ``RETRIEVAL_LLM_CHARS`` through ``app.agent.overlap.model_texts``)."""
        return truncate_text(self.text, LLM_TEXT_CHARS)

    @property
    def location(self) -> str | None:
        """'p. 3', 'pp. 3-4' or 'slide 5' (None when the source has no numbering)."""
        if self.slide_number is not None:
            return f"slide {self.slide_number}"
        if self.page_start is None:
            return None
        if self.page_end is None or self.page_end == self.page_start:
            return f"p. {self.page_start}"
        return f"pp. {self.page_start}-{self.page_end}"


@dataclass(frozen=True)
class TierResult:
    scope: SearchScope
    chunks: list[RetrievedChunk]
    sufficient: bool

    @property
    def best_vector_similarity(self) -> float | None:
        return best_similarity(self.chunks)


# -- pure helpers ----------------------------------------------------------------------------------


def truncate_text(text: str, limit: int) -> str:
    """``text`` cut to about ``limit`` characters at a word boundary, with an ellipsis. Text without
    blanks near the limit (e.g. CJK) is cut hard at the limit."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if not text[limit].isspace():
        boundary = max(cut.rfind(" "), cut.rfind("\n"), cut.rfind("\t"))
        if boundary > limit // 2:
            cut = cut[:boundary]
    return cut.rstrip() + "…"


def build_fts_query(query: str) -> str | None:
    """A safe FTS5 ``MATCH`` expression from free text, or ``None`` when nothing searchable is left.

    Only word tokens survive (so operators, column filters, ``*``, parentheses and quotes in the
    input are plain punctuation and vanish); each token is double-quoted and the tokens are OR-ed
    so a natural-language question still matches, with bm25 ranking the best matches first.
    """
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in _WORD.findall(query):
        token = raw[:MAX_TOKEN_CHARS].casefold()
        if token in seen:
            continue
        seen.add(token)
        tokens.append('"' + token.replace('"', '""') + '"')
        if len(tokens) >= MAX_FTS_TOKENS:
            break
    return " OR ".join(tokens) if tokens else None


def rrf_fuse(rankings: Sequence[Sequence[int]], k: int = RRF_K) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion: score(id) = sum over rankings of 1 / (k + rank), rank from 1.

    Returns ``(id, score)`` best first. Ties are broken by the best (lowest) rank any ranking gave
    the id, then by the order in which ids were first seen, so the result is deterministic.
    """
    scores: dict[int, float] = {}
    best_rank: dict[int, int] = {}
    first_seen: dict[int, int] = {}
    for ranking in rankings:
        seen_here: set[int] = set()
        for position, item in enumerate(ranking, start=1):
            if item in seen_here:
                continue
            seen_here.add(item)
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + position)
            best_rank[item] = min(best_rank.get(item, position), position)
            first_seen.setdefault(item, len(first_seen))
    ordered = sorted(scores, key=lambda i: (-scores[i], best_rank[i], first_seen[i]))
    return [(item, scores[item]) for item in ordered]


def best_similarity(chunks: Sequence[RetrievedChunk]) -> float | None:
    """The highest vector similarity among ``chunks`` (``None`` when none came from the vectors)."""
    return max(
        (c.vector_similarity for c in chunks if c.vector_similarity is not None), default=None
    )


def is_sufficient(chunks: Sequence[RetrievedChunk], *, min_score: float, min_hits: int) -> bool:
    """A tier is sufficient when it has at least ``min_hits`` hits (and at least one) and the best
    vector similarity (1 - cosine distance) among them is at least ``min_score``.

    The fused RRF score is rank based (a lone rank-1 hit is 1/61 whatever its relevance), so it is
    never used here; it only orders results. A chunk found only by keyword search has no
    similarity and cannot satisfy a positive threshold. ``min_score <= 0`` disables the
    similarity check. The LLM judgement is added by the agent in M5."""
    if not chunks or len(chunks) < min_hits:
        return False
    if min_score <= 0.0:
        return True
    best = best_similarity(chunks)
    return best is not None and best >= min_score


# -- SQL -------------------------------------------------------------------------------------------


def _scope_sql(scope: SearchScope) -> tuple[str, dict[str, Any]]:
    if scope.level == "module":
        return "c.module_id = :scope_id", {"scope_id": scope.target_id}
    if scope.level == "block":
        return "c.block_id = :scope_id", {"scope_id": scope.target_id}
    return "1 = 1", {}


def where_clause(
    scope: SearchScope, access: AccessContext, filters: SearchFilters
) -> tuple[str, dict[str, Any]]:
    """The one predicate shared by both retrieval queries (aliases: c = chunks, d = documents)."""
    scope_sql, params = _scope_sql(scope)
    guard = access_filter(access)
    parts = [scope_sql, "d.status = 'ready'", f"({guard.sql})"]
    params.update(guard.params)
    if filters.week is not None:
        parts.append("d.week = :week")
        params["week"] = filters.week
    if filters.document_id is not None:
        parts.append("d.id = :document_id")
        params["document_id"] = filters.document_id
    return " AND ".join(parts), params


def vector_sql(where: str) -> str:
    return (
        "SELECT v.rowid AS seq, v.distance AS distance FROM chunk_vectors v "
        "WHERE v.embedding MATCH :query_vector AND k = :k AND v.rowid IN ("
        "SELECT c.seq FROM chunks c JOIN documents d ON d.id = c.document_id "
        f"WHERE {where}) ORDER BY v.distance"
    )


def fts_sql(where: str) -> str:
    return (
        "SELECT chunks_fts.rowid AS seq FROM chunks_fts "
        "JOIN chunks c ON c.seq = chunks_fts.rowid "
        "JOIN documents d ON d.id = c.document_id "
        f"WHERE chunks_fts MATCH :fts_query AND {where} "
        "ORDER BY bm25(chunks_fts) LIMIT :k"
    )


_DETAIL_SELECT = (
    "SELECT c.seq, c.id, c.document_id, d.title AS document_title, "
    "d.original_filename, d.week, c.module_id, c.block_id, "
    "y.title AS year_title, b.title AS block_title, m.title AS module_title, "
    "c.page_start, c.page_end, c.slide_number, c.section_path, c.text, "
    "c.ordinal, c.token_count, c.metadata "
    "FROM chunks c JOIN documents d ON d.id = c.document_id "
    "JOIN modules m ON m.id = c.module_id JOIN blocks b ON b.id = c.block_id "
    "JOIN years y ON y.id = c.year_id "
)


def _build(
    row: Any, *, score: float, tier: Level, similarity: float | None = None
) -> RetrievedChunk:
    raw_meta = row.metadata
    if isinstance(raw_meta, str):
        try:
            raw_meta = json.loads(raw_meta)
        except ValueError:
            raw_meta = {}
    return RetrievedChunk(
        chunk_id=row.id,
        document_id=row.document_id,
        document_title=row.document_title,
        document_filename=row.original_filename,
        module_id=row.module_id,
        block_id=row.block_id,
        year_title=row.year_title,
        block_title=row.block_title,
        module_title=row.module_title,
        page_start=row.page_start,
        page_end=row.page_end,
        slide_number=row.slide_number,
        section_path=row.section_path,
        text=row.text,
        score=score,
        tier=tier,
        week=row.week,
        ordinal=row.ordinal,
        token_count=row.token_count,
        metadata=raw_meta if isinstance(raw_meta, dict) else {},
        vector_similarity=similarity,
    )


# -- search ----------------------------------------------------------------------------------------


async def hybrid_search(
    db: AsyncSession,
    *,
    access: AccessContext,
    scope: SearchScope,
    query: str,
    query_vector: Sequence[float],
    filters: SearchFilters | None = None,
    top_k: int = 8,
    candidates: int = CANDIDATES,
) -> list[RetrievedChunk]:
    """The best ``top_k`` chunks of ``scope`` for ``query``, best first. The caller has already
    checked the embedding invariant and embedded the query."""
    filters = filters or SearchFilters()
    where, params = where_clause(scope, access, filters)
    params = {**params, "k": candidates}

    vector_rows = (
        await db.execute(
            text(vector_sql(where)),
            {**params, "query_vector": sqlite_vec.serialize_float32(list(query_vector))},
        )
    ).all()
    similarity = {int(r.seq): 1.0 - float(r.distance) for r in vector_rows}

    fts_query = build_fts_query(query)
    fts_ranking: list[int] = []
    if fts_query is not None:
        fts_rows = (
            await db.execute(text(fts_sql(where)), {**params, "fts_query": fts_query})
        ).all()
        fts_ranking = [int(r.seq) for r in fts_rows]

    fused = rrf_fuse([[int(r.seq) for r in vector_rows], fts_ranking])[:top_k]
    if not fused:
        return []
    scores = dict(fused)
    detail = text(_DETAIL_SELECT + "WHERE c.seq IN :seqs").bindparams(
        bindparam("seqs", expanding=True)
    )
    rows = {int(r.seq): r for r in (await db.execute(detail, {"seqs": list(scores)})).all()}
    return [
        _build(rows[seq], score=score, tier=scope.level, similarity=similarity.get(seq))
        for seq, score in fused
        if seq in rows
    ]


async def tier_scopes(db: AsyncSession, start: SearchScope) -> list[SearchScope]:
    """The scopes to try in order: module -> its block -> all; block -> all; all alone."""
    if start.level == "all":
        return [start]
    if start.level == "block":
        return [start, SearchScope("all")]
    block_id = (
        await db.execute(
            text("SELECT block_id FROM modules WHERE id = :id"), {"id": start.target_id}
        )
    ).scalar_one_or_none()
    scopes = [start]
    if block_id is not None:
        scopes.append(SearchScope("block", str(block_id)))
    scopes.append(SearchScope("all"))
    return scopes


async def search_tiered(
    db: AsyncSession,
    *,
    access: AccessContext,
    start: SearchScope,
    query: str,
    query_vector: Sequence[float],
    filters: SearchFilters | None = None,
    top_k: int = 8,
    min_score: float = 0.0,
    min_hits: int = 1,
) -> list[TierResult]:
    """Search ``start`` first and widen tier by tier until one is sufficient. Returns every tier
    that was searched, in order; the last one is the result to use (sufficient or not)."""
    results: list[TierResult] = []
    for scope in await tier_scopes(db, start):
        chunks = await hybrid_search(
            db,
            access=access,
            scope=scope,
            query=query,
            query_vector=query_vector,
            filters=filters,
            top_k=top_k,
        )
        sufficient = is_sufficient(chunks, min_score=min_score, min_hits=min_hits)
        results.append(TierResult(scope, chunks, sufficient))
        if sufficient:
            break
    return results


async def get_passage(
    db: AsyncSession, *, access: AccessContext, chunk_id: str
) -> RetrievedChunk | None:
    """One chunk by id with the same status and access predicate as search (``None`` when the
    chunk does not exist, its document is not ``ready`` or the caller may not read it)."""
    where, params = where_clause(SearchScope("all"), access, SearchFilters())
    sql = _DETAIL_SELECT + f"WHERE c.id = :chunk_id AND {where}"
    row = (await db.execute(text(sql), {**params, "chunk_id": chunk_id})).first()
    if row is None:
        return None
    return _build(row, score=0.0, tier="all")
