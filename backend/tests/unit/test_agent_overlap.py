"""Trimming the chunk overlap from what the model sees (app/agent/overlap.py)."""

from app.agent.overlap import MIN_OVERLAP_CHARS, model_texts, trim_overlap
from app.retrieval.hybrid import RetrievedChunk, truncate_text
from tests.unit.test_agent_citations import chunk


def words(start: int, stop: int) -> str:
    return " ".join(f"word{i:03d}" for i in range(start, stop))


def part(doc: str, ordinal: int, text: str) -> RetrievedChunk:
    return chunk(ordinal + 1, document_id=doc, ordinal=ordinal, text=text)


def test_the_duplicated_start_of_the_later_chunk_is_removed() -> None:
    earlier, later = words(0, 40), words(30, 70)
    assert trim_overlap(earlier, later) == words(40, 70)


def test_no_overlap_means_no_change() -> None:
    assert trim_overlap(words(0, 40), words(50, 90)) == words(50, 90)


def test_matching_is_done_on_whitespace_normalised_text() -> None:
    earlier = words(0, 40).replace(" ", "\n", 5)
    later = words(30, 70).replace(" ", "  ")
    assert trim_overlap(earlier, later) == words(40, 70).replace(" ", "  ")


def test_a_short_accidental_match_is_ignored() -> None:
    earlier = "Some long text that ends with see above"
    later = "see above and then a completely different continuation follows here"
    assert len("see above") < MIN_OVERLAP_CHARS
    assert trim_overlap(earlier, later) == later


def test_a_chunk_that_is_only_overlap_keeps_its_last_word() -> None:
    earlier = words(0, 40)
    later = words(20, 40)  # entirely contained in the tail of the earlier chunk
    assert trim_overlap(earlier, later) != ""


def test_consecutive_chunks_of_one_document_are_trimmed_whatever_the_rank_order() -> None:
    a, b = part("d", 3, words(0, 40)), part("d", 4, words(30, 70))
    expected = [words(0, 40), words(40, 70)]
    assert model_texts([a, b], 5000) == expected
    assert model_texts([b, a], 5000) == [words(40, 70), words(0, 40)]


def test_the_stored_text_is_untouched() -> None:
    a, b = part("d", 0, words(0, 40)), part("d", 1, words(30, 70))
    model_texts([a, b], 5000)
    assert b.text == words(30, 70)


def test_non_adjacent_chunks_are_not_trimmed() -> None:
    a, c = part("d", 0, words(0, 40)), part("d", 2, words(30, 70))
    assert model_texts([a, c], 5000) == [words(0, 40), words(30, 70)]


def test_chunks_of_different_documents_are_not_trimmed() -> None:
    a, b = part("d1", 0, words(0, 40)), part("d2", 1, words(30, 70))
    assert model_texts([a, b], 5000) == [words(0, 40), words(30, 70)]


def test_a_chain_trims_each_chunk_against_the_previous_one() -> None:
    a, b, c = part("d", 0, words(0, 40)), part("d", 1, words(30, 70)), part("d", 2, words(60, 100))
    assert model_texts([a, b, c], 5000) == [words(0, 40), words(40, 70), words(70, 100)]


def test_an_overlap_the_model_cannot_see_is_not_trimmed() -> None:
    a, b = part("d", 0, words(0, 40)), part("d", 1, words(30, 70))
    budget = len(words(0, 40)) - 20  # the earlier chunk is cut before its tail
    out = model_texts([a, b], budget)
    assert out[1] == truncate_text(words(30, 70), budget)


def test_trimming_is_idempotent() -> None:
    earlier, later = words(0, 40), words(30, 70)
    once = trim_overlap(earlier, later)
    assert trim_overlap(earlier, once) == once


def test_texts_are_cut_at_a_word_boundary_with_an_ellipsis() -> None:
    out = model_texts([part("d", 0, words(0, 200))], 100)[0]
    assert out.endswith("…") and len(out) <= 101
    assert out[:-1].split()[-1] in words(0, 200).split()  # no half word
    assert model_texts([part("d", 0, "short")], 100) == ["short"]
