from app.agent.citations import (
    Registry,
    citation_payload,
    cited_payloads,
    strip_markers,
    validate_citations,
)
from app.retrieval.hybrid import RetrievedChunk


def chunk(n: int, **overrides: object) -> RetrievedChunk:
    values: dict[str, object] = {
        "chunk_id": f"chunk{n}",
        "document_id": f"doc{n}",
        "document_title": f"Lecture {n}",
        "document_filename": f"lecture{n}.pdf",
        "module_id": "m1",
        "block_id": "b1",
        "year_title": "Year 1",
        "block_title": "Block 1",
        "module_title": "Module 1",
        "page_start": n,
        "page_end": n,
        "slide_number": None,
        "section_path": "1 Intro",
        "text": f"Passage number {n}. " * 5,
        "score": 0.03,
        "tier": "module",
    }
    values.update(overrides)
    return RetrievedChunk(**values)  # type: ignore[arg-type]


def registry(count: int = 3) -> Registry:
    return Registry.from_chunks([chunk(i) for i in range(1, count + 1)])


def test_registry_numbers_chunks_in_order() -> None:
    reg = registry(3)
    assert list(reg.material) == ["C1", "C2", "C3"]
    assert reg.material["C2"].chunk_id == "chunk2"
    assert "C3" in reg and "C4" not in reg and "W1" not in reg


def test_valid_markers_are_kept_and_reported_in_order_of_first_appearance() -> None:
    result = validate_citations("B [C2] then A [C1] and B again [C2].", registry())
    assert result.text == "B [C2] then A [C1] and B again [C2]."
    assert result.cited == ["C2", "C1"]
    assert result.dropped == []


def test_unknown_markers_are_removed_with_their_leading_space() -> None:
    result = validate_citations("Claim one [C9]. Claim two [C1], and [W1] web.", registry())
    assert result.text == "Claim one. Claim two [C1], and web."
    assert result.cited == ["C1"]
    assert result.dropped == ["C9", "W1"]


def test_adjacent_and_multi_digit_markers() -> None:
    reg = registry(12)
    result = validate_citations("Both [C1][C12] hold, not [C13][C2].", reg)
    assert result.text == "Both [C1][C12] hold, not [C2]."
    assert result.cited == ["C1", "C12", "C2"]
    assert result.dropped == ["C13"]


def test_grouped_markers_keep_only_the_valid_ids() -> None:
    result = validate_citations("See [C1, C9] and [C8; C9] and [C2,C3].", registry())
    assert result.text == "See [C1] and and [C2][C3]."
    assert result.cited == ["C1", "C2", "C3"]
    assert result.dropped == ["C9", "C8", "C9"]


def test_malformed_markers_are_plain_text() -> None:
    text = "Odd [C], [C-1], [c1], [C 1], [CC1], [C1a], (C1), C1, [[C1x]] and [C1234]."
    result = validate_citations(text, registry())
    assert result.text == text
    assert result.cited == [] and result.dropped == []


def test_markers_inside_code_are_left_alone_and_do_not_count() -> None:
    text = "Use `arr[C9]` and\n```\nx = d[C1]\ny = d[C7]\n```\nthen [C2] [C8]."
    result = validate_citations(text, registry())
    assert result.text == "Use `arr[C9]` and\n```\nx = d[C1]\ny = d[C7]\n```\nthen [C2]."
    assert result.cited == ["C2"]
    assert result.dropped == ["C8"]


def test_an_unterminated_fence_protects_the_rest() -> None:
    result = validate_citations("a [C1]\n```\nb [C9]", registry())
    assert result.text == "a [C1]\n```\nb [C9]"
    assert result.cited == ["C1"]


def test_empty_registry_drops_everything() -> None:
    result = validate_citations("Nothing [C1] here [W2].", Registry())
    assert result.text == "Nothing here."
    assert result.cited == [] and result.dropped == ["C1", "W2"]


def test_strip_markers_for_the_stored_memory() -> None:
    assert strip_markers("Heaps [C1][C2] are trees [W3].") == "Heaps are trees."
    assert strip_markers("keep `[C1]` code") == "keep `[C1]` code"


def test_only_cited_sources_are_returned_as_structured_payloads() -> None:
    reg = registry(3)
    payloads = cited_payloads(reg, ["C3", "C1", "C9"])
    assert [p["marker"] for p in payloads] == ["C3", "C1"]
    first = payloads[0]
    assert first["type"] == "material"
    assert first["chunk_id"] == "chunk3" and first["document_id"] == "doc3"
    assert first["document_title"] == "Lecture 3"
    assert first["path"] == "Year 1 › Block 1 › Module 1"
    assert first["location"] == "p. 3" and first["page_start"] == 3
    assert first["section_path"] == "1 Intro"


def test_payload_snippet_is_flattened_and_truncated() -> None:
    long = chunk(1, text="word  " * 200)
    snippet = citation_payload("C1", long)["snippet"]
    assert len(snippet) <= 301 and snippet.endswith("…") and "  " not in snippet
    slide = citation_payload("C1", chunk(1, slide_number=4, page_start=None, page_end=None))
    assert slide["location"] == "slide 4"
