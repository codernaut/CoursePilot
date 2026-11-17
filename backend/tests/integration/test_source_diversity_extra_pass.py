"""Source diversity, extra pass: when the cap bites and the other documents are missing from the
first pass's candidate pool, one more hybrid pass without the capped documents finds them."""

from collections import Counter
from typing import Any

import pytest
from sqlalchemy import text

from app.models.enums import DocumentStatus
from app.retrieval import hybrid
from app.retrieval.access import AccessContext
from app.retrieval.hybrid import (
    RetrievedChunk,
    SearchFilters,
    SearchScope,
    best_similarity,
    hybrid_search,
    search_tiered,
    where_clause,
)
from tests.ingest_support import Env
from tests.search_support import access_for_faculty, ingest, module_id, set_status

QUERY = "binary heap largest item root of the tree"
TOP_K = 6
CAP = 3
MARKER = "tuberculosis"
FILLER = "history notation amortised analysis memory layout array indexing parent child " * 3


def guide_markdown(label: str, count: int = 30) -> bytes:
    sections = [f"## Case {n}\n\n{QUERY}. {QUERY}. {label} {n}" for n in range(1, count + 1)]
    return (f"# {label}\n\n" + "\n\n".join(sections) + "\n").encode()


def textbook_markdown() -> bytes:
    sections = [
        f"## Chapter {n}\n\nkitchen flour sugar butter oven recipe baking dough {n}"
        for n in range(1, 7)
    ]
    sections.append(
        f"## Chapter 7\n\nThe largest item of a heap sits at the root of the tree ({MARKER})."
    )
    return ("# Textbook\n\n" + "\n\n".join(sections) + "\n").encode()


async def chunk_count(env: Env, document_id: str) -> int:
    async with env.database.read_session() as db:
        value = await db.execute(
            text("SELECT count(*) FROM chunks WHERE document_id = :d"), {"d": document_id}
        )
        return int(value.scalar_one())


async def search(
    env: Env, *, cap: int | None, scope: SearchScope | None = None, top_k: int = TOP_K
) -> list[RetrievedChunk]:
    vector = await env.embedder.embed_query(QUERY)
    async with env.database.read_session() as db:
        return await hybrid_search(
            db,
            access=await access_for_faculty(env),
            scope=scope or SearchScope("module", env.module_id),
            query=QUERY,
            query_vector=vector,
            top_k=top_k,
            max_per_document=cap,
        )


@pytest.fixture
async def real_case(ingest_env: Env) -> Env:
    guide = await ingest(ingest_env, guide_markdown("Guide"), title="Guide")
    await ingest(ingest_env, textbook_markdown(), title="Textbook")
    assert await chunk_count(ingest_env, guide.id) > hybrid.CANDIDATES
    return ingest_env


async def test_premise_the_textbook_is_absent_from_the_first_pass_pool(real_case: Env) -> None:
    pool = await search(real_case, cap=None, top_k=100)
    assert {h.document_title for h in pool} == {"Guide"}


async def test_the_extra_pass_finds_the_relevant_textbook_chunk(real_case: Env) -> None:
    hits = await search(real_case, cap=CAP)
    assert len(hits) == TOP_K
    titles = [h.document_title for h in hits]
    assert (
        titles[:CAP] == ["Guide"] * CAP
    )  # first pass's picks keep their order, then the extra pass
    assert Counter(titles)["Textbook"] == TOP_K - CAP
    assert MARKER in hits[CAP].text  # the relevant chunk, not a random one
    assert hits[CAP].vector_similarity is not None  # similarity reported for extra-pass hits
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)


async def test_sufficiency_uses_the_merged_list(real_case: Env) -> None:
    vector = await real_case.embedder.embed_query(QUERY)
    async with real_case.database.read_session() as db:
        tiers = await search_tiered(
            db,
            access=await access_for_faculty(real_case),
            start=SearchScope("module", real_case.module_id),
            query=QUERY,
            query_vector=vector,
            top_k=TOP_K,
            max_per_document=CAP,
        )
    assert len(tiers) == 1
    chunks = tiers[0].chunks
    assert any(c.document_title == "Textbook" for c in chunks)
    assert tiers[0].best_vector_similarity == best_similarity(chunks)
    assert all(c.vector_similarity is not None for c in chunks if c.document_title == "Textbook")


async def test_the_extra_pass_never_leaves_the_scope(real_case: Env) -> None:
    other = await module_id(real_case, 1, 1, 2)
    await ingest(real_case, textbook_markdown(), title="Elsewhere", module=other)
    hits = await search(real_case, cap=CAP)
    assert "Elsewhere" not in {h.document_title for h in hits}
    assert {h.module_id for h in hits} == {real_case.module_id}


async def test_the_extra_pass_respects_the_document_status(real_case: Env) -> None:
    async with real_case.database.read_session() as db:
        textbook_id = (
            await db.execute(text("SELECT id FROM documents WHERE title = 'Textbook'"))
        ).scalar_one()
    await set_status(real_case, str(textbook_id), DocumentStatus.DELETING)
    hits = await search(real_case, cap=CAP)
    assert {h.document_title for h in hits} == {"Guide"}


async def test_several_capped_documents_are_all_excluded(ingest_env: Env) -> None:
    await ingest(ingest_env, guide_markdown("Guide A", 15), title="Guide A")
    await ingest(ingest_env, guide_markdown("Guide B", 15), title="Guide B")
    await ingest(ingest_env, textbook_markdown(), title="Textbook")
    hits = await search(ingest_env, cap=2)
    counts = Counter(h.document_title for h in hits)
    assert len(hits) == TOP_K
    assert counts["Guide A"] <= 2 and counts["Guide B"] <= 2
    assert counts["Textbook"] == TOP_K - counts["Guide A"] - counts["Guide B"] >= 2
    assert any(MARKER in h.text for h in hits)


def test_where_clause_binds_the_excluded_documents_and_keeps_every_predicate() -> None:
    access = AccessContext(user_id="u", is_active=True)
    where, params = where_clause(
        SearchScope("module", "m1"), access, SearchFilters(week="W1"), ["d1", "d2'; DROP --"]
    )
    assert "d.id NOT IN :excluded_docs" in where
    assert "d.status = 'ready'" in where and "c.module_id = :scope_id" in where
    assert "d.week = :week" in where
    assert params["excluded_docs"] == ["d1", "d2'; DROP --"]
    assert "DROP" not in where
    plain, plain_params = where_clause(SearchScope("module", "m1"), access, SearchFilters())
    assert "excluded_docs" not in plain and "excluded_docs" not in plain_params


async def count_passes(monkeypatch: pytest.MonkeyPatch, env: Env, cap: int) -> int:
    calls: list[int] = []
    original = hybrid._candidates

    async def counting(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return await original(*args, **kwargs)

    monkeypatch.setattr(hybrid, "_candidates", counting)
    await search(env, cap=cap)
    return len(calls)


async def test_no_extra_pass_when_no_document_hits_the_cap(
    real_case: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert await count_passes(monkeypatch, real_case, cap=100) == 1


async def test_one_extra_pass_when_the_cap_bites(
    real_case: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert await count_passes(monkeypatch, real_case, cap=CAP) == 2


async def test_no_extra_pass_for_a_single_document_module(
    ingest_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await ingest(ingest_env, guide_markdown("Guide"), title="Guide")
    assert await count_passes(monkeypatch, ingest_env, cap=CAP) == 1
    hits = await search(ingest_env, cap=CAP)
    assert len(hits) == TOP_K and {h.document_title for h in hits} == {"Guide"}
