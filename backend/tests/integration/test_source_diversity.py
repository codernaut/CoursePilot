"""Source diversity: a short case guide worded like the question must not crowd a large reference
text out of the final top-k (real search with the fake embedder)."""

from collections import Counter

import pytest

from app.retrieval.hybrid import RetrievedChunk, SearchScope, hybrid_search, search_tiered
from tests.ingest_support import Env
from tests.search_support import access_for_faculty, ingest

QUERY = "binary heap largest item root of the tree"
TOP_K = 6
CAP = 3


def guide_markdown() -> bytes:
    sections = [
        f"## Case {n}\n\nbinary heap largest item root of the tree answer {n}" for n in range(1, 13)
    ]
    return ("# Facilitator guide\n\n" + "\n\n".join(sections) + "\n").encode()


def textbook_markdown() -> bytes:
    filler = "history notation amortised analysis memory layout array indexing parent child"
    sections = [
        f"## Chapter {n}\n\nA heap stores its biggest element at the top. {filler} detail {n}"
        for n in range(1, 9)
    ]
    return ("# Textbook\n\n" + "\n\n".join(sections) + "\n").encode()


async def search(env: Env, cap: int | None) -> list[RetrievedChunk]:
    vector = await env.embedder.embed_query(QUERY)
    async with env.database.read_session() as db:
        return await hybrid_search(
            db,
            access=await access_for_faculty(env),
            scope=SearchScope("module", env.module_id),
            query=QUERY,
            query_vector=vector,
            top_k=TOP_K,
            max_per_document=cap,
        )


@pytest.fixture
async def two_documents(ingest_env: Env) -> Env:
    await ingest(ingest_env, guide_markdown(), title="Guide", module=ingest_env.module_id)
    await ingest(ingest_env, textbook_markdown(), title="Textbook", module=ingest_env.module_id)
    return ingest_env


async def test_without_a_cap_the_guide_takes_more_than_its_share(two_documents: Env) -> None:
    hits = await search(two_documents, None)
    assert Counter(h.document_title for h in hits)["Guide"] > CAP


async def test_the_cap_lets_the_textbook_reach_the_final_hits(two_documents: Env) -> None:
    hits = await search(two_documents, CAP)
    counts = Counter(h.document_title for h in hits)
    assert len(hits) == TOP_K
    assert counts["Guide"] == CAP and counts["Textbook"] == TOP_K - CAP
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)


async def test_a_cap_of_one_still_fills_top_k(two_documents: Env) -> None:
    hits = await search(two_documents, 1)
    assert len(hits) == TOP_K
    assert {"Guide", "Textbook"} <= {h.document_title for h in hits}


async def test_a_single_document_module_still_gets_top_k(ingest_env: Env) -> None:
    await ingest(ingest_env, guide_markdown(), title="Guide", module=ingest_env.module_id)
    hits = await search(ingest_env, CAP)
    assert len(hits) == TOP_K and {h.document_title for h in hits} == {"Guide"}


async def tiered(env: Env, cap: int | None) -> list[RetrievedChunk]:
    vector = await env.embedder.embed_query(QUERY)
    async with env.database.read_session() as db:
        tiers = await search_tiered(
            db,
            access=await access_for_faculty(env),
            start=SearchScope("module", env.module_id),
            query=QUERY,
            query_vector=vector,
            top_k=TOP_K,
            max_per_document=cap,
        )
    assert [t.scope.level for t in tiers] == ["module"]  # capping never skips or adds a tier
    return tiers[-1].chunks


async def test_search_tiered_passes_the_cap_through(two_documents: Env) -> None:
    capped = Counter(h.document_title for h in await tiered(two_documents, CAP))
    assert capped["Guide"] == CAP and capped["Textbook"] == TOP_K - CAP
    uncapped = Counter(h.document_title for h in await tiered(two_documents, None))
    assert uncapped["Guide"] > CAP


async def test_search_tiered_single_document_module_still_gets_top_k(ingest_env: Env) -> None:
    await ingest(ingest_env, guide_markdown(), title="Guide", module=ingest_env.module_id)
    hits = await tiered(ingest_env, CAP)
    assert len(hits) == TOP_K and {h.document_title for h in hits} == {"Guide"}


async def test_capped_hits_stay_inside_the_scope(two_documents: Env) -> None:
    hits = await search(two_documents, 1)
    assert {h.module_id for h in hits} == {two_documents.module_id}
