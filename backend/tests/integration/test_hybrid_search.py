"""Hybrid search against a real SQLite database with the fake embedder: scope, status filters, the
access predicate inside the SQL, tiered widening and the embedding invariant."""

from collections.abc import Sequence

import pytest
from sqlalchemy import event, text

from app.core.errors import AppError
from app.ingestion.embedder import EmbeddingError, EmbeddingNotConfiguredError, FakeEmbedder
from app.models.enums import DocumentStatus
from app.retrieval import hybrid
from app.retrieval.access import AccessContext
from app.retrieval.hybrid import (
    PATH_SEPARATOR,
    RetrievedChunk,
    SearchFilters,
    SearchScope,
    hybrid_search,
    search_tiered,
)
from app.retrieval.query_embedding import QueryEmbedder
from app.services import search as search_service
from tests.ingest_support import Env
from tests.search_support import (
    Corpus,
    access_for_faculty,
    build_corpus,
    ingest,
    module_id,
    set_status,
)

ALL = SearchScope("all")


async def run(
    corpus: Corpus,
    query: str,
    scope: SearchScope = ALL,
    *,
    access: AccessContext | None = None,
    filters: SearchFilters | None = None,
    top_k: int = 8,
) -> list[RetrievedChunk]:
    env = corpus.env
    vector = await env.embedder.embed_query(query)
    async with env.database.read_session() as db:
        return await hybrid_search(
            db,
            access=access or corpus.access,
            scope=scope,
            query=query,
            query_vector=vector,
            filters=filters,
            top_k=top_k,
        )


def titles(hits: Sequence[RetrievedChunk]) -> list[str]:
    return [h.document_title for h in hits]


@pytest.fixture
async def corpus(ingest_env: Env) -> Corpus:
    return await build_corpus(ingest_env)


async def test_finds_the_matching_chunk_with_full_metadata(corpus: Corpus) -> None:
    hits = await run(corpus, "binary heap root of the tree")
    assert hits and hits[0].document_title == "Heaps"
    top = hits[0]
    assert top.path == PATH_SEPARATOR.join(["Year 1", "Block 1", "Module 1"])
    assert top.document_filename == "heaps.md"
    assert top.week == "Week 1"
    assert top.section_path == "Heaps"
    assert "binary heap" in top.text
    assert top.score > 0 and top.vector_similarity is not None
    assert top.tier == "all"
    assert top.metadata["kind"] == "body"
    assert top.page_start is None and top.slide_number is None
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)


async def test_keyword_only_match_is_found_through_fts(corpus: Corpus) -> None:
    hits = await run(corpus, "glucose")
    assert hits[0].document_title == "Plants"


async def test_top_k_limits_results(corpus: Corpus) -> None:
    assert len(await run(corpus, "the of and a", top_k=2)) == 2


async def test_module_scope_never_returns_other_modules(corpus: Corpus) -> None:
    hits = await run(corpus, "glucose oxygen", SearchScope("module", corpus.same_module))
    assert set(titles(hits)) <= {"Heaps"}
    assert all(h.module_id == corpus.same_module and h.tier == "module" for h in hits)
    hits = await run(corpus, "dijkstra priority queue", SearchScope("module", corpus.same_module))
    assert "Graphs" not in titles(hits)


async def test_block_scope_stays_inside_the_block(corpus: Corpus) -> None:
    hits = await run(corpus, "heap graph path root", SearchScope("block", corpus.block_id))
    assert set(titles(hits)) == {"Heaps", "Graphs"}
    assert all(h.block_id == corpus.block_id for h in hits)


async def test_all_scope_returns_every_module(corpus: Corpus) -> None:
    hits = await run(corpus, "heap graph glucose debit credit root path oxygen")
    assert set(titles(hits)) == {"Heaps", "Graphs", "Plants", "Ledgers"}


@pytest.mark.parametrize(
    "status",
    [
        DocumentStatus.DELETING,
        DocumentStatus.FAILED,
        DocumentStatus.PROCESSING,
        DocumentStatus.UPLOADED,
    ],
)
async def test_documents_that_are_not_ready_are_never_returned(
    corpus: Corpus, status: DocumentStatus
) -> None:
    # the chunks, vectors and FTS rows all still exist; only the status hides the document
    await set_status(corpus.env, corpus.docs["Plants"], status)
    assert "Plants" not in titles(await run(corpus, "glucose chloroplasts sunlight"))
    assert "Plants" not in titles(
        await run(corpus, "glucose", SearchScope("module", corpus.other_block))
    )
    await set_status(corpus.env, corpus.docs["Plants"], DocumentStatus.READY)
    assert "Plants" in titles(await run(corpus, "glucose chloroplasts sunlight"))


async def test_failed_document_with_old_chunks_but_no_vectors_is_hidden(corpus: Corpus) -> None:
    """After reindex-all the old chunks and FTS rows stay until the document is re-processed."""
    env = corpus.env
    await set_status(env, corpus.docs["Ledgers"], DocumentStatus.FAILED)
    async with env.database.session() as db:
        await db.execute(
            text(
                "DELETE FROM chunk_vectors WHERE rowid IN "
                "(SELECT seq FROM chunks WHERE document_id = :d)"
            ),
            {"d": corpus.docs["Ledgers"]},
        )
        await db.commit()
    assert await env.count("chunks_fts") >= 4
    assert "Ledgers" not in titles(await run(corpus, "double entry bookkeeping debit credit"))


async def test_week_and_document_filters(corpus: Corpus) -> None:
    query = "heap graph glucose debit root path oxygen"
    week1 = await run(corpus, query, filters=SearchFilters(week="Week 1"))
    assert set(titles(week1)) == {"Heaps", "Plants"}
    one_doc = await run(corpus, query, filters=SearchFilters(document_id=corpus.docs["Graphs"]))
    assert set(titles(one_doc)) == {"Graphs"}
    both = await run(
        corpus,
        query,
        filters=SearchFilters(week="Week 1", document_id=corpus.docs["Graphs"]),
    )
    assert both == []
    assert await run(corpus, query, filters=SearchFilters(week="Week 99")) == []


async def test_query_with_only_punctuation_still_searches_vectors(corpus: Corpus) -> None:
    hits = await run(corpus, "?!?")
    assert isinstance(hits, list)  # no FTS query, no crash


async def test_hostile_query_text_is_harmless(corpus: Corpus) -> None:
    for query in ('" OR 1=1 --', "chunks_fts:*", "heap NEAR/2 root)", "'; DROP TABLE chunks;--"):
        await run(corpus, query)
    assert await corpus.env.count("chunks") >= 4


# --- the access predicate lives in the SQL -------------------------------------------------------


async def test_anonymous_and_inactive_contexts_get_nothing(corpus: Corpus) -> None:
    assert await corpus.env.count("chunks") >= 4
    assert await run(corpus, "binary heap", access=AccessContext.anonymous()) == []
    assert await run(corpus, "binary heap", access=AccessContext("someone", False)) == []


async def test_user_deactivated_after_the_context_was_built_gets_nothing(corpus: Corpus) -> None:
    assert await run(corpus, "binary heap")  # allowed
    async with corpus.env.database.session() as db:
        await db.execute(text("UPDATE users SET is_active = 0"))
        await db.commit()
    assert await run(corpus, "binary heap") == []  # the same context is refused inside the query


async def test_unknown_user_id_gets_nothing(corpus: Corpus) -> None:
    assert await run(corpus, "binary heap", access=AccessContext("no-such-user", True)) == []


async def test_both_queries_contain_the_scope_status_and_access_predicates(corpus: Corpus) -> None:
    env = corpus.env
    statements: list[str] = []
    engine = env.database.engine.sync_engine

    def capture(conn: object, cursor: object, statement: str, *a: object) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        await run(corpus, "binary heap", SearchScope("module", corpus.same_module))
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    vector = next(s for s in statements if "chunk_vectors" in s and "MATCH" in s)
    fts = next(s for s in statements if "chunks_fts MATCH" in s)
    for sql in (vector, fts):
        assert "d.status = 'ready'" in sql
        assert "u_access.is_active = 1" in sql
        assert "c.module_id = ?" in sql
    assert "rowid IN (SELECT c.seq FROM chunks c JOIN documents d" in vector
    assert "JOIN documents d ON d.id = c.document_id" in fts


async def test_scope_is_applied_before_top_k_not_after(ingest_env: Env) -> None:
    """Many strong matches in one module must not crowd out a weaker match in the scoped module."""
    env = ingest_env
    other = await module_id(env, 1, 1, 2)
    for i in range(30):
        await ingest(
            env, f"# Trees {i}\n\ntree traversal visits nodes {i}.\n".encode(), title=f"T{i}"
        )
    await ingest(
        env, b"# Forest\n\nA tree is a graph without cycles.\n", title="Forest", module=other
    )
    access = await access_for_faculty(env)
    vector = await env.embedder.embed_query("tree traversal nodes")
    async with env.database.read_session() as db:
        hits = await hybrid_search(
            db,
            access=access,
            scope=SearchScope("module", other),
            query="tree traversal nodes",
            query_vector=vector,
            top_k=5,
        )
    assert titles(hits) == ["Forest"]


# --- tiered widening -----------------------------------------------------------------------------


async def tiered(
    corpus: Corpus, query: str, start: SearchScope, **kw: float
) -> list[hybrid.TierResult]:
    env = corpus.env
    vector = await env.embedder.embed_query(query)
    async with env.database.read_session() as db:
        return await search_tiered(
            db,
            access=corpus.access,
            start=start,
            query=query,
            query_vector=vector,
            **kw,  # type: ignore[arg-type]
        )


async def test_sufficient_module_tier_stops_the_search(corpus: Corpus) -> None:
    tiers = await tiered(corpus, "binary heap root", SearchScope("module", corpus.same_module))
    assert [t.scope.level for t in tiers] == ["module"]
    assert tiers[0].sufficient and tiers[0].chunks[0].document_title == "Heaps"


async def test_insufficient_module_widens_to_block_then_all(corpus: Corpus) -> None:
    # nothing in the module matches; the block has the Graphs document; "all" has Plants
    scope = SearchScope("module", corpus.same_module)
    tiers = await tiered(corpus, "xyzzy", scope, min_hits=1, min_score=1.0)
    assert [t.scope.level for t in tiers] == ["module", "block", "all"]
    assert not any(t.sufficient for t in tiers)  # min_score 1.0 can never be reached
    assert [t.scope.target_id for t in tiers] == [corpus.same_module, corpus.block_id, None]


async def test_widening_stops_at_the_first_sufficient_tier(corpus: Corpus) -> None:
    scope = SearchScope("module", corpus.same_module)
    query = "dijkstra priority queue weighted graph edges"
    # Make the module tier insufficient by requiring more hits than the one-document module has.
    tiers = await tiered(corpus, query, scope, min_hits=2)
    levels = [t.scope.level for t in tiers]
    assert levels[0] == "module" and not tiers[0].sufficient
    assert len(tiers) >= 2
    assert tiers[1].scope.level == "block"
    assert "Graphs" in titles(tiers[1].chunks)
    assert titles(tiers[0].chunks) in ([], ["Heaps"])


async def test_block_start_goes_block_then_all_and_all_is_single(corpus: Corpus) -> None:
    block = await tiered(corpus, "xyzzy", SearchScope("block", corpus.block_id), min_score=1.0)
    assert [t.scope.level for t in block] == ["block", "all"]
    single = await tiered(corpus, "glucose", ALL)
    assert [t.scope.level for t in single] == ["all"]


async def test_sufficiency_follows_the_best_vector_similarity(corpus: Corpus) -> None:
    scope = SearchScope("module", corpus.same_module)
    query = "binary heap root"
    low = await tiered(corpus, query, scope, min_score=0.01)
    assert [t.scope.level for t in low] == ["module"] and low[0].sufficient
    best = low[0].best_vector_similarity
    assert best is not None and 0.01 <= best < 1.0
    # a threshold above the best similarity widens, even though RRF scores are unchanged
    high = await tiered(corpus, query, scope, min_score=min(best + 0.01, 1.0))
    assert [t.scope.level for t in high][:2] == ["module", "block"]
    assert not high[0].sufficient


async def test_search_service_applies_the_threshold_without_widening(corpus: Corpus) -> None:
    env = corpus.env
    qe = QueryEmbedder(env.settings, factory=lambda: env.embedder)
    strict = env.settings.model_copy(update={"retrieval_min_score": 1.0})
    for settings, expected in ((env.settings, True), (strict, False)):
        async with env.database.read_session() as db:
            outcome = await search_service.search(
                db, settings=settings, embedder=qe, access=corpus.access,
                query="binary heap root", scope=SearchScope("module", corpus.same_module),
            )  # fmt: skip
        assert outcome.chunks and outcome.tiers[0].sufficient is expected


async def test_chunks_are_labelled_with_the_tier_that_found_them(corpus: Corpus) -> None:
    scope = SearchScope("module", corpus.same_module)
    tiers = await tiered(corpus, "dijkstra priority queue", scope, min_hits=2, min_score=0.0)
    for tier in tiers:
        assert all(c.tier == tier.scope.level for c in tier.chunks)


# --- invariant and provider (service level) -----------------------------------------------------


async def test_search_service_checks_the_index_before_embedding(corpus: Corpus) -> None:
    env = corpus.env
    other = env.settings.model_copy(update={"embedding_model": "other"})
    qe = QueryEmbedder(other, factory=lambda: FakeEmbedder(model="other", dim=other.embedding_dim))
    async with env.database.read_session() as db:
        with pytest.raises(AppError) as caught:
            await search_service.search(
                db, settings=other, embedder=qe, access=corpus.access, query="heap", scope=ALL
            )
    assert caught.value.code == "index_mismatch" and caught.value.status_code == 503
    assert qe.cache is not None and len(qe.cache) == 0  # nothing was embedded


async def test_search_service_without_provider_is_503_provider_not_configured(
    corpus: Corpus,
) -> None:
    env = corpus.env

    def broken() -> FakeEmbedder:
        raise EmbeddingNotConfiguredError("The Gemini embedding provider is not configured.")

    qe = QueryEmbedder(env.settings, factory=broken)
    async with env.database.read_session() as db:
        with pytest.raises(AppError) as caught:
            await search_service.search(
                db,
                settings=env.settings,
                embedder=qe,
                access=corpus.access,
                query="heap",
                scope=ALL,
            )
    assert caught.value.code == "provider_not_configured" and caught.value.status_code == 503


async def test_search_service_empty_and_oversized_queries_are_422(corpus: Corpus) -> None:
    env = corpus.env
    qe = QueryEmbedder(env.settings, factory=lambda: env.embedder)
    async with env.database.read_session() as db:
        for query in ("   ", "\x00\x01", "x" * 1001):
            with pytest.raises(AppError) as caught:
                await search_service.search(
                    db, settings=env.settings, embedder=qe, access=corpus.access, query=query,
                    scope=ALL,
                )  # fmt: skip
            assert caught.value.status_code == 422


async def test_embedding_provider_failure_is_502(corpus: Corpus) -> None:
    env = corpus.env

    class Failing(FakeEmbedder):
        async def embed_query(self, text: str) -> list[float]:
            raise EmbeddingError("Gemini rejected the key sk-SECRET (HTTP 403)", retryable=True)

    qe = QueryEmbedder(
        env.settings, factory=lambda: Failing(model=env.embedder.model, dim=env.embedder.dim)
    )
    async with env.database.read_session() as db:
        with pytest.raises(AppError) as caught:
            await search_service.search(
                db,
                settings=env.settings,
                embedder=qe,
                access=corpus.access,
                query="heap",
                scope=ALL,
            )
    assert caught.value.status_code == 502
    assert caught.value.code == "embedding_unavailable"
    assert "SECRET" not in caught.value.message and "Gemini" not in caught.value.message


async def test_search_service_widen_flag_returns_all_tiers(corpus: Corpus) -> None:
    env = corpus.env
    qe = QueryEmbedder(env.settings, factory=lambda: env.embedder)
    async with env.database.read_session() as db:
        outcome = await search_service.search(
            db, settings=env.settings, embedder=qe, access=corpus.access, query="glucose",
            scope=SearchScope("module", corpus.same_module), widen=True,
        )  # fmt: skip
    assert outcome.tiers[-1].scope.level in ("module", "block", "all")
    assert outcome.chunks and outcome.chunks[0].document_title in ("Plants", "Heaps")


async def test_get_passage_applies_status_and_access(corpus: Corpus) -> None:
    env = corpus.env
    hit = (await run(corpus, "binary heap"))[0]
    async with env.database.read_session() as db:
        found = await hybrid.get_passage(db, access=corpus.access, chunk_id=hit.chunk_id)
        assert found is not None and found.text == hit.text
        assert await hybrid.get_passage(db, access=corpus.access, chunk_id="nope") is None
        assert (
            await hybrid.get_passage(db, access=AccessContext.anonymous(), chunk_id=hit.chunk_id)
            is None
        )
    await set_status(env, hit.document_id, DocumentStatus.DELETING)
    async with env.database.read_session() as db:
        assert await hybrid.get_passage(db, access=corpus.access, chunk_id=hit.chunk_id) is None


async def test_vectors_and_fts_stay_aligned(corpus: Corpus) -> None:
    env = corpus.env
    assert (
        await env.count("chunk_vectors")
        == await env.count("chunks")
        == await env.count("chunks_fts")
    )
