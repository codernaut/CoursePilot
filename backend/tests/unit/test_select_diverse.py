"""``select_diverse``: the per-document cap on the final top-k (pure function)."""

from app.retrieval.hybrid import select_diverse


def fused_of(docs: list[str]) -> tuple[list[tuple[int, float]], dict[int, str]]:
    """A fused ranking whose i-th entry (seq i + 1) belongs to ``docs[i]``, scores descending."""
    fused = [(i + 1, 1.0 / (60 + i + 1)) for i in range(len(docs))]
    return fused, {i + 1: doc for i, doc in enumerate(docs)}


def seqs(selected: list[tuple[int, float]]) -> list[int]:
    return [seq for seq, _ in selected]


def test_the_cap_is_respected_and_other_documents_get_the_slots() -> None:
    fused, doc_of = fused_of(["g"] * 8 + ["t", "t", "o"])
    selected = select_diverse(fused, doc_of, 6, 4)
    docs = [doc_of[s] for s in seqs(selected)]
    assert docs.count("g") == 4 and docs.count("t") == 2 and len(selected) == 6
    assert seqs(selected) == [1, 2, 3, 4, 9, 10]


def test_skipped_chunks_refill_when_there_are_too_few_documents() -> None:
    fused, doc_of = fused_of(["g"] * 5 + ["t"])
    selected = select_diverse(fused, doc_of, 6, 2)
    assert len(selected) == 6
    assert seqs(selected) == [1, 2, 3, 4, 5, 6]  # fused order, nothing lost


def test_a_single_document_module_still_returns_top_k() -> None:
    fused, doc_of = fused_of(["g"] * 10)
    selected = select_diverse(fused, doc_of, 6, 4)
    assert seqs(selected) == [1, 2, 3, 4, 5, 6]


def test_the_output_is_ordered_by_fused_score() -> None:
    fused, doc_of = fused_of(["g", "g", "g", "t", "g", "t", "o", "g"])
    selected = select_diverse(fused, doc_of, 6, 3)
    scores = [score for _, score in selected]
    assert scores == sorted(scores, reverse=True)
    assert seqs(selected) == sorted(seqs(selected))
    assert seqs(selected) == [1, 2, 3, 4, 6, 7]


def test_refilled_chunks_are_placed_by_fused_score_not_appended() -> None:
    fused, doc_of = fused_of(["g", "g", "g", "t"])
    selected = select_diverse(fused, doc_of, 4, 2)
    assert seqs(selected) == [1, 2, 3, 4]  # chunk 3 (skipped, refilled) stays before chunk 4


def test_it_is_deterministic_for_equal_scores() -> None:
    fused = [(7, 0.5), (3, 0.5), (9, 0.5), (1, 0.5)]
    doc_of = {7: "a", 3: "a", 9: "b", 1: "b"}
    first = select_diverse(fused, doc_of, 3, 1)
    assert first == select_diverse(fused, doc_of, 3, 1)
    assert seqs(first) == [7, 3, 9]  # input order wins; the refill takes the first skipped


def test_top_k_smaller_than_the_cap() -> None:
    fused, doc_of = fused_of(["g"] * 5 + ["t"])
    assert seqs(select_diverse(fused, doc_of, 2, 4)) == [1, 2]


def test_a_cap_of_one_gives_one_chunk_per_document_first() -> None:
    fused, doc_of = fused_of(["g", "g", "t", "t", "o"])
    assert seqs(select_diverse(fused, doc_of, 3, 1)) == [1, 3, 5]
    assert seqs(select_diverse(fused, doc_of, 4, 1)) == [1, 2, 3, 5]  # refill from the skipped


def test_fewer_candidates_than_top_k() -> None:
    fused, doc_of = fused_of(["g", "g"])
    assert seqs(select_diverse(fused, doc_of, 6, 1)) == [1, 2]
    assert select_diverse([], {}, 6, 4) == []
