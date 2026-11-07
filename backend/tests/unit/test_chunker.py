"""Chunker: size bounds, overlap, sentence boundaries, page tracking, slide merging, headers."""

import itertools
import re

from app.ingestion.chunker import DEFAULT_PARAMS, DocChunk, chunk_document, split_sentences
from app.ingestion.parsers.base import ParsedSection
from app.ingestion.tokens import count_tokens

PROGRAMME = "Year 1 > Block 2 > Algorithms"
SENTENCE_END = re.compile(r"[.!?]$")


def sentence(n: int, tag: str = "") -> str:
    # ~22 tokens each, capitalised, ends with a full stop
    return f"Sentence {n}{tag} explains how the merge procedure combines two sorted halves quickly."


def prose(count: int, tag: str = "", start: int = 0) -> str:
    return " ".join(sentence(i, tag) for i in range(start, start + count))


def chunks_of(sections: list[ParsedSection]) -> list[DocChunk]:
    return chunk_document(sections, document_title="Lecture 3", programme_path=PROGRAMME)


# --- sentence splitting ------------------------------------------------------------------------


def test_split_sentences_keeps_abbreviations_and_decimals_together() -> None:
    text = "See Fig. 3 for details, e.g. the cost is 3.5 units. Dr. Smith agrees! Is it true? Yes."
    assert split_sentences(text) == [
        "See Fig. 3 for details, e.g. the cost is 3.5 units.",
        "Dr. Smith agrees!",
        "Is it true?",
        "Yes.",
    ]


# --- sizes, overlap, boundaries ----------------------------------------------------------------


def test_short_document_is_one_chunk_with_header_and_metadata() -> None:
    chunks = chunks_of([ParsedSection("A short note about sorting.", heading_path=("Sorting",))])
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.ordinal == 0
    assert chunk.text == "A short note about sorting."
    assert chunk.embed_text == (
        "Year 1 > Block 2 > Algorithms | Lecture 3 | Sorting\n\nA short note about sorting."
    )
    assert chunk.section_path == "Sorting"
    assert chunk.token_count == count_tokens(chunk.text)
    meta = chunk.metadata
    assert meta["kind"] == "body"
    assert meta["chunker"]["max_tokens"] == DEFAULT_PARAMS.max_tokens
    assert meta["chunker"]["overlap_min_tokens"] == 80
    assert meta["chunker"]["token_counter"] == "heuristic-v1"


def test_header_without_a_section_path_has_two_parts() -> None:
    chunk = chunks_of([ParsedSection("Plain text.")])[0]
    assert chunk.section_path is None
    assert chunk.embed_text.startswith("Year 1 > Block 2 > Algorithms | Lecture 3\n\nPlain")


def test_long_text_is_split_within_bounds_with_sentence_aligned_overlap() -> None:
    chunks = chunks_of([ParsedSection(prose(200))])  # about 4,400 tokens
    assert len(chunks) >= 5
    p = DEFAULT_PARAMS
    for index, chunk in enumerate(chunks):
        assert chunk.token_count <= p.max_tokens
        assert chunk.ordinal == index
        if index < len(chunks) - 1:
            assert chunk.token_count >= 500
        # never mid-sentence: whole sentences only
        assert chunk.text.startswith("Sentence ") and SENTENCE_END.search(chunk.text)
    for previous, current in itertools.pairwise(chunks):
        prev_sentences = split_sentences(previous.text)
        cur_sentences = split_sentences(current.text)
        shared = []
        for s in cur_sentences:
            if s in prev_sentences:
                shared.append(s)
            else:
                break
        assert shared, "consecutive chunks overlap"
        assert prev_sentences[-len(shared) :] == shared  # the overlap is the previous tail
        overlap_tokens = count_tokens(" ".join(shared))
        assert p.overlap_min_tokens <= overlap_tokens <= p.overlap_max_tokens
    # nothing lost: every sentence appears in some chunk
    all_text = " ".join(c.text for c in chunks)
    assert all(sentence(i) in all_text for i in range(200))


def test_a_tiny_trailing_remainder_is_folded_into_the_previous_chunk() -> None:
    # 650-token target: 31 sentences fill chunk 1, leaving too little for a chunk of its own
    chunks = chunks_of([ParsedSection(prose(34))])
    assert all(c.token_count <= DEFAULT_PARAMS.max_tokens for c in chunks)
    last_new = [s for s in split_sentences(chunks[-1].text)]
    assert len(last_new) >= 3 or len(chunks) == 1


def test_hard_wrapped_lines_are_rejoined_not_split_mid_sentence() -> None:
    text = (
        "Sorting algorithms put the items\nof a list into order and\nthey differ in speed.\nNext."
    )
    chunk = chunks_of([ParsedSection(text)])[0]
    assert chunk.text.startswith(
        "Sorting algorithms put the items of a list into order and they differ in speed."
    )


def test_a_single_endless_sentence_is_hard_split_and_flagged() -> None:
    chunks = chunks_of([ParsedSection(" ".join(f"word{i}" for i in range(3000)))])
    assert len(chunks) > 1
    assert all(c.token_count <= DEFAULT_PARAMS.max_tokens for c in chunks)
    assert all(c.metadata.get("hard_split") is True for c in chunks)


# --- page tracking -----------------------------------------------------------------------------


def test_page_start_and_end_follow_the_sentences_in_each_chunk() -> None:
    pages = [ParsedSection(prose(30, f"p{n}"), page=n) for n in range(1, 6)]
    chunks = chunks_of(pages)
    assert len(chunks) >= 3
    for chunk in chunks:
        assert chunk.page_start is not None and chunk.page_end is not None
        found = {int(m) for m in re.findall(r"Sentence \d+p(\d)", chunk.text)}
        assert chunk.page_start == min(found)
        assert chunk.page_end == max(found)
    assert any(c.page_start != c.page_end for c in chunks), "a chunk crosses a page boundary"
    assert chunks[0].page_start == 1 and chunks[-1].page_end == 5
    assert all(c.slide_number is None for c in chunks)


def test_a_new_heading_starts_a_new_chunk_and_pages_stay_separate() -> None:
    chunks = chunks_of(
        [
            ParsedSection("Alpha text here.", page=1, heading_path=("A",)),
            ParsedSection("Beta text here.", page=2, heading_path=("B",)),
        ]
    )
    assert [(c.text, c.page_start, c.page_end, c.section_path) for c in chunks] == [
        ("Alpha text here.", 1, 1, "A"),
        ("Beta text here.", 2, 2, "B"),
    ]


# --- slides ------------------------------------------------------------------------------------


def slide(n: int, text: str, *, parent: str = "Unit", title: str | None = None, **kw: object):  # type: ignore[no-untyped-def]
    path = (parent, title or f"Slide {n}")
    return ParsedSection(text, slide_number=n, heading_path=path, **kw)  # type: ignore[arg-type]


def test_a_normal_slide_is_one_chunk_and_notes_are_separate() -> None:
    body = prose(6)
    sections = [
        slide(1, body, title="Merge"),
        slide(1, "Remember to demo this.", title="Merge", kind="speaker_notes"),
    ]
    chunks = chunks_of(sections)
    assert [(c.slide_number, c.metadata["kind"], c.section_path) for c in chunks] == [
        (1, "slide", "Unit > Merge"),
        (1, "speaker_notes", "Unit > Merge > Speaker notes"),
    ]
    assert chunks[0].text == body
    assert chunks[0].page_start is None


def test_a_slide_table_stays_with_its_slide() -> None:
    sections = [
        slide(2, "Costs of sorting.", title="Costs"),
        slide(2, "Sort | Time\nMerge | n log n", title="Costs", kind="table"),
    ]
    chunks = chunks_of(sections)
    assert len(chunks) == 1
    assert chunks[0].text == "Costs of sorting.\n\nSort | Time\nMerge | n log n"


def test_tiny_consecutive_slides_are_merged_within_the_same_section() -> None:
    sections = [slide(n, f"Point {n} is short.") for n in range(1, 6)]
    chunks = chunks_of(sections)
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.slide_number == 1
    assert chunk.metadata["slide_end"] == 5
    assert all(f"Point {n} is short." in chunk.text for n in range(1, 6))
    assert chunk.section_path is not None and "Slide 1" in chunk.section_path


def test_slides_under_different_parents_or_with_big_neighbours_are_not_merged() -> None:
    sections = [
        slide(1, "Tiny one.", parent="Part A"),
        slide(2, "Tiny two.", parent="Part B"),
        slide(3, prose(12), parent="Part B"),  # not small: stands alone
        slide(4, "Tiny four.", parent="Part B"),
    ]
    chunks = chunks_of(sections)
    assert [c.slide_number for c in chunks] == [1, 2, 3, 4]
    assert all("slide_end" not in c.metadata for c in chunks)


def test_merging_stops_when_the_merged_chunk_would_exceed_the_target() -> None:
    small = " ".join(f"Short sentence {i} about merging." for i in range(14))  # ~140 tokens
    assert count_tokens(small) < DEFAULT_PARAMS.merge_slides_below_tokens
    chunks = chunks_of([slide(n, small) for n in range(1, 11)])
    assert all(c.token_count <= DEFAULT_PARAMS.target_tokens for c in chunks)
    assert len(chunks) > 1
    covered: list[int] = []
    for c in chunks:
        assert c.slide_number is not None
        covered.extend(range(c.slide_number, c.metadata.get("slide_end", c.slide_number) + 1))
    assert covered == list(range(1, 11))


def test_an_oversized_slide_is_split_but_keeps_its_slide_number() -> None:
    chunks = chunks_of([slide(7, prose(120))])
    assert len(chunks) >= 3
    assert {c.slide_number for c in chunks} == {7}
    assert all(c.token_count <= DEFAULT_PARAMS.max_tokens for c in chunks)


# --- tables ------------------------------------------------------------------------------------


def test_table_rows_are_never_split() -> None:
    rows = [f"Row {i} | alpha beta gamma delta epsilon | {i * 7}" for i in range(400)]
    chunks = chunks_of([ParsedSection("\n".join(rows), kind="table", heading_path=("T",))])
    assert len(chunks) > 1
    seen: list[str] = []
    for chunk in chunks:
        assert chunk.metadata["kind"] == "table"
        assert chunk.token_count <= DEFAULT_PARAMS.max_tokens
        lines = chunk.text.split("\n")
        assert all(line in rows for line in lines)
        seen.extend(lines)
    assert seen == rows  # no overlap for tables, nothing lost or reordered


def test_empty_input_gives_no_chunks() -> None:
    assert chunks_of([]) == []
    assert chunks_of([ParsedSection("   ")]) == []


# --- review: hostile input ---------------------------------------------------------------------


def test_many_skipped_boundaries_split_in_linear_time() -> None:
    """'A. B. C. ...' skips every boundary (single-letter abbreviations); the look-back at the
    previous word must be bounded, or the work grows with the square of the text length."""
    import time

    text = "A. B. C. D. " * 20_000  # 240 kB
    started = time.monotonic()
    assert split_sentences(text) == [text.strip()]
    assert time.monotonic() - started < 5.0


def test_long_word_before_a_dot_is_not_mistaken_for_an_abbreviation() -> None:
    """The look-back is bounded, so a long word ending in an abbreviation must still not match."""
    text = ("x" * 100 + "etc") + ". Next sentence starts here."
    assert split_sentences(text) == [("x" * 100 + "etc") + ".", "Next sentence starts here."]


def test_cjk_run_without_punctuation_respects_the_token_limit() -> None:
    chunks = chunks_of([ParsedSection(text="中" * 5000)])
    assert len(chunks) > 1
    assert all(c.token_count <= DEFAULT_PARAMS.max_tokens for c in chunks)
    assert "".join(c.text for c in chunks).count("中") >= 5000
