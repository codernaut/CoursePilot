"""FTS sanitiser, RRF fusion, sufficiency, the query-embedding cache and the access predicate."""

from pathlib import Path

import pytest

from app.ingestion.embedder import FakeEmbedder
from app.retrieval.access import DENY_ALL, AccessContext, access_filter
from app.retrieval.hybrid import (
    LLM_TEXT_CHARS,
    PATH_SEPARATOR,
    RetrievedChunk,
    SearchScope,
    build_fts_query,
    is_sufficient,
    rrf_fuse,
)
from app.retrieval.query_embedding import QueryEmbedder, QueryEmbeddingCache, normalise_query
from tests.conftest import make_settings


def chunk(score: float, text: str = "x", **kw: object) -> RetrievedChunk:
    values: dict[str, object] = {
        "chunk_id": "c",
        "document_id": "d",
        "document_title": "T",
        "document_filename": "t.md",
        "module_id": "m",
        "block_id": "b",
        "year_title": "Year 1",
        "block_title": "Block 1",
        "module_title": "Module 1",
        "page_start": None,
        "page_end": None,
        "slide_number": None,
        "section_path": None,
        "text": text,
        "score": score,
        "tier": "module",
    }
    values.update(kw)
    return RetrievedChunk(**values)  # type: ignore[arg-type]


# --- FTS sanitiser -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("merge sort", '"merge" OR "sort"'),
        ("Merge MERGE sort", '"merge" OR "sort"'),
        ('say "hello" now', '"say" OR "hello" OR "now"'),
        ("cats AND dogs", '"cats" OR "and" OR "dogs"'),  # operators are plain words here
        ("a NEAR b", '"a" OR "near" OR "b"'),
        ("embed_text: secret", '"embed_text" OR "secret"'),
        ("title:secret*", '"title" OR "secret"'),
        ("(a OR b)* ^c", '"a" OR "or" OR "b" OR "c"'),
        ("   ", None),
        ("", None),
        ("!!! ??? ---", None),
        ('"', None),
        ("naïve Über 数据", '"naïve" OR "über" OR "数据"'),
    ],
)
def test_build_fts_query(query: str, expected: str | None) -> None:
    assert build_fts_query(query) == expected


def test_fts_query_never_contains_raw_syntax() -> None:
    result = build_fts_query('x" OR "y NEAR(a b) col:val* -neg ^start {a b}') or ""
    for part in result.split(" OR "):
        assert part.startswith('"') and part.endswith('"')
        assert set(part[1:-1]) <= set("abcdefghijklmnopqrstuvwxyz_0123456789")


def test_fts_query_is_bounded() -> None:
    result = build_fts_query(" ".join(f"w{i}" for i in range(500))) or ""
    assert result.count(" OR ") == 31
    assert len(build_fts_query("a" * 5000) or "") == 64 + 2


# --- RRF -----------------------------------------------------------------------------------------


def test_rrf_orders_by_combined_rank() -> None:
    fused = rrf_fuse([[1, 2, 3], [3, 2, 4]])
    assert [i for i, _ in fused] == [3, 2, 1, 4]
    scores = dict(fused)
    assert scores[2] == pytest.approx(1 / 62 + 1 / 62)
    assert scores[3] == pytest.approx(1 / 63 + 1 / 61)


def test_rrf_disjoint_lists_interleave_by_rank() -> None:
    fused = rrf_fuse([[1, 2], [3, 4]])
    assert [i for i, _ in fused] == [1, 3, 2, 4]  # equal scores: first list wins the tie


def test_rrf_single_and_empty_lists() -> None:
    assert [i for i, _ in rrf_fuse([[5, 6, 7]])] == [5, 6, 7]
    assert rrf_fuse([[], []]) == []
    assert rrf_fuse([]) == []


def test_rrf_ties_are_deterministic() -> None:
    assert [i for i, _ in rrf_fuse([[1], [2]])] == [1, 2]
    assert [i for i, _ in rrf_fuse([[7, 8], [8, 7]])] == [7, 8]


def test_rrf_k_parameter_changes_weights() -> None:
    low = dict(rrf_fuse([[1]], k=1))
    high = dict(rrf_fuse([[1]], k=100))
    assert low[1] == pytest.approx(0.5)
    assert high[1] == pytest.approx(1 / 101)


def test_rrf_ignores_duplicates_inside_one_ranking() -> None:
    assert dict(rrf_fuse([[1, 1, 1]]))[1] == pytest.approx(1 / 61)


# --- sufficiency, truncation ---------------------------------------------------------------------


def test_is_sufficient_uses_vector_similarity_not_the_rank_score() -> None:
    assert not is_sufficient([], min_score=0.0, min_hits=0)
    # default threshold 0.0: any hit counts, whatever its similarity (even none)
    assert is_sufficient([chunk(0.02)], min_score=0.0, min_hits=1)
    assert is_sufficient([chunk(0.02, vector_similarity=-0.2)], min_score=0.0, min_hits=1)
    # the fused rank score is irrelevant: a high RRF score with low similarity is insufficient
    assert not is_sufficient([chunk(0.03, vector_similarity=0.2)], min_score=0.3, min_hits=1)
    assert is_sufficient([chunk(0.001, vector_similarity=0.6)], min_score=0.3, min_hits=1)
    # the best similarity in the tier counts; boundary is inclusive
    hits = [chunk(0.03, vector_similarity=0.1), chunk(0.02, vector_similarity=0.3)]
    assert is_sufficient(hits, min_score=0.3, min_hits=2)
    assert not is_sufficient([chunk(0.5, vector_similarity=0.99)], min_score=0.0, min_hits=2)


def test_keyword_only_hits_have_no_similarity_and_fail_a_positive_threshold() -> None:
    keyword_only = [chunk(0.03), chunk(0.02)]
    assert not is_sufficient(keyword_only, min_score=0.3, min_hits=1)
    mixed = [chunk(0.03), chunk(0.02, vector_similarity=0.5)]
    assert is_sufficient(mixed, min_score=0.3, min_hits=1)


def test_llm_text_is_truncated_but_text_is_complete() -> None:
    long = chunk(0.1, "word " * 1000)
    assert len(long.text) == 5000
    assert len(long.llm_text) <= LLM_TEXT_CHARS + 1
    assert long.llm_text.endswith("…")
    assert chunk(0.1, "short").llm_text == "short"


def test_location_and_path() -> None:
    assert chunk(0.1).location is None
    assert chunk(0.1).path == PATH_SEPARATOR.join(["Year 1", "Block 1", "Module 1"])
    assert chunk(0.1, page_start=3, page_end=3).location == "p. 3"
    assert chunk(0.1, page_start=3, page_end=4).location == "pp. 3-4"
    assert chunk(0.1, slide_number=5).location == "slide 5"


def test_scope_validation() -> None:
    SearchScope("all")
    SearchScope("module", "abc")
    with pytest.raises(ValueError, match="target id"):
        SearchScope("module")
    with pytest.raises(ValueError, match="target id"):
        SearchScope("all", "abc")


# --- access predicate ----------------------------------------------------------------------------


def test_access_filter_denies_anonymous_and_inactive() -> None:
    assert access_filter(AccessContext.anonymous()).sql == DENY_ALL
    assert access_filter(AccessContext(user_id="u1", is_active=False)).sql == DENY_ALL
    assert access_filter(AccessContext(user_id=None, is_active=True)).sql == DENY_ALL


def test_access_filter_allows_active_users_inside_sql() -> None:
    allowed = access_filter(AccessContext(user_id="u1", is_active=True))
    assert "users" in allowed.sql and "is_active" in allowed.sql
    assert allowed.params == {"access_user_id": "u1"}


# --- query embedding cache -----------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


KEY = ("fake", "m", 4, "q")


def test_cache_hit_miss_and_ttl_expiry() -> None:
    clock = Clock()
    cache = QueryEmbeddingCache(ttl_s=60, max_entries=10, clock=clock)
    assert cache.get(KEY) is None
    cache.put(KEY, [1.0])
    assert cache.get(KEY) == [1.0]
    clock.now += 59
    assert cache.get(KEY) == [1.0]
    clock.now += 2
    assert cache.get(KEY) is None
    assert len(cache) == 0


def test_cache_is_bounded_and_evicts_least_recently_used() -> None:
    cache = QueryEmbeddingCache(ttl_s=60, max_entries=2, clock=Clock())
    cache.put(("p", "m", 1, "a"), [1.0])
    cache.put(("p", "m", 1, "b"), [2.0])
    assert cache.get(("p", "m", 1, "a")) == [1.0]  # touch a
    cache.put(("p", "m", 1, "c"), [3.0])
    assert len(cache) == 2
    assert cache.get(("p", "m", 1, "b")) is None
    assert cache.get(("p", "m", 1, "a")) == [1.0]


def test_cache_drops_expired_entries_before_evicting_live_ones() -> None:
    clock = Clock()
    cache = QueryEmbeddingCache(ttl_s=60, max_entries=2, clock=clock)
    cache.put(("p", "m", 1, "old"), [1.0])
    clock.now += 30
    cache.put(("p", "m", 1, "live"), [2.0])
    clock.now += 40  # "old" is now expired, "live" is not
    cache.put(("p", "m", 1, "new"), [3.0])
    assert cache.get(("p", "m", 1, "live")) == [2.0]
    assert cache.get(("p", "m", 1, "new")) == [3.0]


def test_normalise_query() -> None:
    assert normalise_query("  Merge   SORT ") == "merge sort"


async def test_query_embedder_caches_identical_queries_and_keys_by_model(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    fake = FakeEmbedder(model="m1", dim=8)
    qe = QueryEmbedder(settings, factory=lambda: fake)
    first = await qe.embed("Merge sort")
    again = await qe.embed("  merge   SORT ")
    assert first == again and fake.query_calls == 1
    await qe.embed("heap sort")
    assert fake.query_calls == 2
    other = FakeEmbedder(model="m2", dim=8)
    qe2 = QueryEmbedder(settings, factory=lambda: other, cache=qe.cache)
    await qe2.embed("merge sort")
    assert other.query_calls == 1  # a different model never reuses the cached vector
