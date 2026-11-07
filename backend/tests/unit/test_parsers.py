"""Parsers: pages/slides, heading paths, header/footer removal, hyphen repair, limits."""

import pytest

from app.ingestion.parsers import (
    NeedsOcrError,
    ParseError,
    ParseLimitError,
    ParseLimits,
    parse_document,
)
from app.ingestion.parsers.base import clean_text
from tests.docfiles import (
    add_zip64_eocd,
    forge_eocd,
    make_docx,
    make_encrypted_pdf,
    make_pptx,
    make_text_pdf,
)

LIMITS = ParseLimits()
BODIES = [
    "Sorting arranges items in order.\nMerge sort splits the list in halves.",
    "Quick sort picks a pivot value.\nPartitioning places smaller items left.",
    "Heap sort builds a binary heap first.\nThen it extracts the maximum item.",
    "Radix sort processes digits one by one.\nIt is stable and not comparison based.",
]


# --- PDF -------------------------------------------------------------------------------------


def test_pdf_one_section_per_page_with_page_numbers() -> None:
    parsed = parse_document("pdf", make_text_pdf(BODIES), LIMITS)
    assert parsed.page_count == 4
    assert [s.page for s in parsed.sections] == [1, 2, 3, 4]
    assert all(s.slide_number is None and s.kind == "body" for s in parsed.sections)
    assert "Merge sort splits the list in halves." in parsed.sections[0].text
    assert "Radix sort" in parsed.sections[3].text


def test_pdf_removes_repeating_headers_and_footers_and_page_numbers() -> None:
    data = make_text_pdf(BODIES, header="CS101 Lecture Notes", footer="Page")
    parsed = parse_document("pdf", data, LIMITS)
    text = "\n".join(s.text for s in parsed.sections)
    assert "CS101 Lecture Notes" not in text
    assert "Page " not in text
    assert "Quick sort picks a pivot value." in text  # the body is kept


def test_pdf_keeps_margin_text_that_does_not_repeat() -> None:
    data = make_text_pdf(BODIES, header="CS101 Lecture Notes")
    data_unique = make_text_pdf(["One page only."], header="CS101 Lecture Notes")
    assert "CS101" not in " ".join(s.text for s in parse_document("pdf", data, LIMITS).sections)
    # With fewer than three pages there is no basis to call it a repeated header.
    assert "CS101" in parse_document("pdf", data_unique, LIMITS).sections[0].text


def test_pdf_repairs_hyphenation_and_reflows_lines() -> None:
    body = "The sorting algo-\nrithm runs fast on\nsmall inputs and an in-\ncomplete list."
    parsed = parse_document("pdf", make_text_pdf([body]), LIMITS)
    text = parsed.sections[0].text
    assert "algorithm runs fast on small inputs" in text
    assert "incomplete list." in text
    assert "\n" not in text


def test_pdf_heading_path_comes_from_the_outline() -> None:
    toc: list[list[object]] = [[1, "Sorting", 1], [2, "Merge sort", 2], [1, "Graphs", 4]]
    parsed = parse_document("pdf", make_text_pdf(BODIES, toc=toc), LIMITS)
    paths = {s.page: s.heading_path for s in parsed.sections}
    assert paths[1] == ("Sorting",)
    assert paths[2] == ("Sorting", "Merge sort")
    assert paths[3] == ("Sorting", "Merge sort")
    assert paths[4] == ("Graphs",)


def test_pdf_without_text_needs_ocr() -> None:
    data = make_text_pdf(["", "", "x"])  # a scanned-looking document: (almost) no text
    with pytest.raises(NeedsOcrError, match="scanned") as info:
        parse_document("pdf", data, LIMITS)
    assert "OCR is not enabled" in str(info.value)


def test_pdf_needs_ocr_with_ocr_flag_is_a_clear_unavailable_error() -> None:
    data = make_text_pdf(["", "", ""])
    with pytest.raises(ParseError, match="OCR support is not available"):
        parse_document("pdf", data, ParseLimits(ocr_enabled=True))


def test_pdf_page_limit() -> None:
    with pytest.raises(ParseLimitError, match="limit is 3"):
        parse_document("pdf", make_text_pdf(BODIES), ParseLimits(max_pages=3))


def test_pdf_time_limit() -> None:
    with pytest.raises(ParseLimitError, match="took longer"):
        parse_document("pdf", make_text_pdf(BODIES), ParseLimits(timeout_s=-1.0))


def test_pdf_password_protected_and_damaged() -> None:
    with pytest.raises(ParseError, match="password"):
        parse_document("pdf", make_encrypted_pdf(), LIMITS)
    with pytest.raises(ParseError, match=r"damaged|cannot be opened"):
        parse_document("pdf", b"%PDF-1.4 this is not a pdf", LIMITS)


# --- PPTX ------------------------------------------------------------------------------------

SLIDES: list[dict[str, object]] = [
    {"title": "Intro", "bullets": ["Goals", "Schedule"], "notes": "Welcome the class."},
    {
        "title": "Costs",
        "bullets": ["Time", "Space"],
        "table": [["Sort", "Time"], ["Merge", "n log n"]],
    },
    {"title": "Only a title"},
]


def test_pptx_sections_slide_numbers_titles_tables_and_notes() -> None:
    parsed = parse_document("pptx", make_pptx(SLIDES), LIMITS)
    assert parsed.page_count == 3
    by_kind = [(s.slide_number, s.kind, s.heading_path) for s in parsed.sections]
    assert by_kind == [
        (1, "body", ("Intro",)),
        (1, "speaker_notes", ("Intro",)),
        (2, "body", ("Costs",)),
        (2, "table", ("Costs",)),
        (3, "body", ("Only a title",)),
    ]
    assert parsed.sections[0].text == "Goals\nSchedule"
    assert parsed.sections[1].text == "Welcome the class."
    assert parsed.sections[3].text == "Sort | Time\nMerge | n log n"
    assert parsed.sections[4].text == "Only a title"  # a title-only slide stays searchable


def test_pptx_slide_limit() -> None:
    with pytest.raises(ParseLimitError, match="3 slides"):
        parse_document("pptx", make_pptx(SLIDES), ParseLimits(max_pages=2))


def test_pptx_declared_size_limit() -> None:
    with pytest.raises(ParseLimitError, match="expands to more than"):
        parse_document("pptx", make_pptx(SLIDES), ParseLimits(max_uncompressed_bytes=1024))


def test_pptx_hostile_zip_directory_is_rejected_before_zipfile_reads_it() -> None:
    forged = forge_eocd(make_pptx(SLIDES), entries=60000)
    with pytest.raises(ParseError, match="too many parts"):
        parse_document("pptx", forged, LIMITS)
    huge = forge_eocd(make_pptx(SLIDES), cd_size=900 * 1024 * 1024)
    with pytest.raises(ParseError, match="oversized directory"):
        parse_document("pptx", huge, LIMITS)
    zip64 = add_zip64_eocd(make_pptx(SLIDES), entries=5_000_000, cd_size=100)
    with pytest.raises(ParseError, match="too many parts"):
        parse_document("pptx", zip64, LIMITS)


def test_pptx_damaged() -> None:
    with pytest.raises(ParseError, match=r"not a valid zip|damaged"):
        parse_document("pptx", b"PK\x03\x04 nothing useful", LIMITS)


# --- DOCX ------------------------------------------------------------------------------------


def test_docx_headings_build_the_heading_path_and_tables_are_sections() -> None:
    data = make_docx(
        [
            ("p", "Preface text before any heading."),
            ("h1", "Sorting"),
            ("p", "Sorting arranges items."),
            ("h2", "Merge sort"),
            ("p", "Merge sort splits lists."),
            ("table", [["Algorithm", "Time"], ["Merge", "n log n"]]),
            ("h1", "Graphs"),
            ("p", "A graph has nodes."),
        ]
    )
    parsed = parse_document("docx", data, LIMITS)
    rows = [(s.heading_path, s.kind, s.text) for s in parsed.sections]
    assert rows == [
        ((), "body", "Preface text before any heading."),
        (("Sorting",), "body", "Sorting arranges items."),
        (("Sorting", "Merge sort"), "body", "Merge sort splits lists."),
        (("Sorting", "Merge sort"), "table", "Algorithm | Time\nMerge | n log n"),
        (("Graphs",), "body", "A graph has nodes."),
    ]
    assert all(s.page is None and s.slide_number is None for s in parsed.sections)
    assert parsed.page_count is None


def test_docx_hostile_directory_is_rejected() -> None:
    forged = forge_eocd(make_docx([("p", "x")]), entries=60000)
    with pytest.raises(ParseError, match="too many parts"):
        parse_document("docx", forged, LIMITS)


# --- Markdown, text, HTML --------------------------------------------------------------------

MARKDOWN = """# Sorting

Intro with **bold** and `code` and a [link](http://example.com).

## Merge sort

- split the list
- merge the halves

```
def merge(): pass
```

| Algorithm | Time |
|-----------|------|
| Merge     | n log n |

<script>alert(1)</script>

# Graphs

Nodes and edges.
"""


def test_markdown_headings_lists_code_and_tables() -> None:
    parsed = parse_document("md", MARKDOWN.encode(), LIMITS)
    rows = [(s.heading_path, s.kind) for s in parsed.sections]
    assert rows == [
        (("Sorting",), "body"),
        (("Sorting", "Merge sort"), "body"),
        (("Sorting", "Merge sort"), "table"),
        (("Graphs",), "body"),
    ]
    assert parsed.sections[0].text == "Intro with bold and code and a link."
    assert "- split the list\n\n- merge the halves" in parsed.sections[1].text
    assert "def merge(): pass" in parsed.sections[1].text
    assert parsed.sections[2].text == "Algorithm | Time\nMerge | n log n"
    assert "alert" not in " ".join(s.text for s in parsed.sections)  # raw HTML is dropped


def test_plain_text_is_one_cleaned_section() -> None:
    data = "﻿First line\x00 with NUL.\r\n\r\n\r\n\r\nSecond‮ paragraph.".encode()
    parsed = parse_document("txt", data, LIMITS)
    assert [s.text for s in parsed.sections] == ["First line with NUL.\n\nSecond paragraph."]
    assert parse_document("txt", b"  \n\n ", LIMITS).sections == []


HTML = b"""<!doctype html><html><head><title>Ignored</title><style>p{color:red}</style>
<script>var secret = 'do not index';</script></head>
<body><h1>Sorting</h1><p>Sorting arranges <b>items</b> in order.</p>
<!-- a comment that must not be indexed -->
<h2>Merge sort</h2><ul><li>split</li><li>merge</li></ul>
<noscript>enable javascript</noscript>
<table><tr><th>Algorithm</th><th>Time</th></tr><tr><td>Merge</td><td>n log n</td></tr></table>
<h1>Graphs</h1><p>Nodes<br>and edges.</p></body></html>"""


def test_html_strips_scripts_styles_comments_and_keeps_structure() -> None:
    parsed = parse_document("html", HTML, LIMITS)
    text = " ".join(s.text for s in parsed.sections)
    for forbidden in ("secret", "color:red", "comment", "enable javascript", "Ignored"):
        assert forbidden not in text
    rows = [(s.heading_path, s.kind, s.text) for s in parsed.sections]
    assert rows[0] == (("Sorting",), "body", "Sorting arranges items in order.")
    assert rows[1] == (("Sorting", "Merge sort"), "body", "split\n\nmerge")
    assert rows[2] == (("Sorting", "Merge sort"), "table", "Algorithm | Time\nMerge | n log n")
    assert rows[3] == (("Graphs",), "body", "Nodes\nand edges.")


def test_html_nesting_limit() -> None:
    deep = b"<div>" * 400 + b"text" + b"</div>" * 400
    with pytest.raises(ParseError, match="nested too deeply"):
        parse_document("html", deep, LIMITS)


def test_unknown_type_and_text_cleaning() -> None:
    with pytest.raises(ParseError, match="cannot be processed"):
        parse_document("exe", b"MZ", LIMITS)
    assert clean_text("a\x00b​c­x\r\ny") == "abcx\ny"
    assert clean_text("a   b\n\n\n\nc  \n") == "a b\n\nc"


def test_deeply_nested_tables_are_parsed_in_linear_time() -> None:
    import time

    html = b"<table><tr><td>cell</td><td>" * 3000 + b"deep text"
    started = time.monotonic()
    try:
        parsed = parse_document("html", html, ParseLimits(timeout_s=60))
        assert any("deep text" in s.text for s in parsed.sections)
    except ParseError:
        pass  # a readable refusal is fine too; hanging is not
    assert time.monotonic() - started < 10


def test_html_nested_and_plain_tables_keep_their_rows() -> None:
    html = (
        b"<h1>T</h1><table><tr><th>a</th><th>b</th></tr>"
        b"<tr><td>1</td><td><table><tr><td>inner</td></tr></table></td></tr></table>"
    )
    rows = [(s.kind, s.text) for s in parse_document("html", html, LIMITS).sections]
    assert ("table", "a | b\n1 |") in rows
    assert ("table", "inner") in rows


def test_pdf_text_budget_stops_reading_pages_early(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.ingestion.parsers import base, pdf

    checks: list[int] = []

    class CountingDeadline(base.Deadline):
        def check(self) -> None:
            checks.append(1)
            super().check()

    monkeypatch.setattr(pdf, "TextBudget", lambda: base.TextBudget(limit=150))
    monkeypatch.setattr(pdf, "Deadline", CountingDeadline)
    data = make_text_pdf([BODIES[0]] * 12)
    with pytest.raises(ParseLimitError, match="far more text"):
        parse_document("pdf", data, LIMITS)
    assert len(checks) < 12  # stopped while still reading pages, not after all twelve


@pytest.mark.parametrize("extension", ["md", "txt", "html"])
def test_text_formats_are_capped_before_parsing(extension: str) -> None:
    small = ParseLimits(max_text_file_bytes=1024)
    with pytest.raises(ParseLimitError, match="larger than"):
        parse_document(extension, b"a " * 1024, small)
    assert parse_document(extension, b"short text", small).sections


def test_zip_formats_cap_is_200_mb() -> None:
    assert ParseLimits().max_uncompressed_bytes == 200 * 1024 * 1024
    assert ParseLimits().max_text_file_bytes == 5 * 1024 * 1024
