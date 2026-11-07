"""PDF parser (pymupdf, AGPL-3.0 like this project).

One section per page. Repeated headers/footers (text blocks in the top or bottom margin that
repeat on most pages, page numbers included) are dropped, words hyphenated at a line break are
rejoined and lines of a block are reflowed into a paragraph. PDFs with (almost) no selectable text
are reported as needing OCR.
"""

import math
import re
from collections import Counter
from typing import Any

from app.ingestion.parsers.base import (
    Deadline,
    NeedsOcrError,
    ParsedDocument,
    ParsedSection,
    ParseError,
    ParseLimitError,
    ParseLimits,
    TextBudget,
    clean_heading,
    clean_text,
)

MARGIN_FRACTION = 0.12  # top/bottom share of the page height where headers/footers live
MIN_PAGES_FOR_MARGIN_REMOVAL = 3
REPEAT_SHARE = 0.5
MIN_CHARS_PER_PAGE = 10  # fewer non-space characters than this = an "empty" page
MAX_TOC_ENTRIES = 5000

_HYPHEN_BREAK = re.compile(r"(?<=[^\W\d_])[-\N{HYPHEN}]\n(?=[^\W\d_A-Z])")
_DIGITS = re.compile(r"\d+")


def _margin_key(text: str) -> str:
    return _DIGITS.sub("#", " ".join(text.lower().split()))


def _reflow(text: str) -> str:
    """Rejoin hyphenated line breaks, then turn line breaks inside the block into spaces."""
    text = _HYPHEN_BREAK.sub("", text)
    return " ".join(line.strip() for line in text.split("\n") if line.strip())


def _outline(doc: Any) -> dict[int, tuple[str, ...]]:
    """Heading path per page (1-based) from the PDF outline, if it has one."""
    try:
        entries = doc.get_toc(simple=True)[:MAX_TOC_ENTRIES]
    except Exception:  # a broken outline is not worth failing the document for
        return {}
    by_page: dict[int, list[tuple[int, str]]] = {}
    for level, title, page in entries:
        if isinstance(page, int) and page >= 1 and isinstance(level, int):
            heading = clean_heading(str(title))
            if heading:
                by_page.setdefault(page, []).append((level, heading))
    result: dict[int, tuple[str, ...]] = {}
    stack: list[tuple[int, str]] = []
    for page in range(1, doc.page_count + 1):
        for level, heading in by_page.get(page, []):
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, heading))
        if stack:
            result[page] = tuple(title for _, title in stack[-6:])
    return result


def parse_pdf(data: bytes, limits: ParseLimits) -> ParsedDocument:
    import pymupdf

    pymupdf.TOOLS.mupdf_display_errors(False)
    deadline = Deadline(limits.timeout_s)
    budget = TextBudget()
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise ParseError("This PDF is damaged or cannot be opened.") from exc
    with doc:
        if doc.needs_pass or doc.is_encrypted:
            raise ParseError("This PDF is password-protected. Remove the password and upload it.")
        page_count = doc.page_count
        if page_count > limits.max_pages:
            raise ParseLimitError(
                f"This PDF has {page_count} pages; the limit is {limits.max_pages}. "
                "Split it into smaller files."
            )
        pages: list[list[tuple[bool, bool, str]]] = []  # (in header zone, in footer zone, text)
        for index in range(page_count):
            deadline.check()
            try:
                page = doc[index]
                height = float(page.rect.height) or 1.0
                raw_blocks = page.get_text("blocks", sort=True)
            except Exception as exc:
                raise ParseError(f"Page {index + 1} of this PDF could not be read.") from exc
            blocks: list[tuple[bool, bool, str]] = []
            for block in raw_blocks:
                if block[6] != 0:  # 0 = text, 1 = image
                    continue
                text = clean_text(str(block[4]))
                if not text:
                    continue
                budget.spend(text)  # as pages are read, not afterwards (a text bomb never grows)
                blocks.append(
                    (
                        block[3] <= height * MARGIN_FRACTION,
                        block[1] >= height * (1 - MARGIN_FRACTION),
                        text,
                    )
                )
            pages.append(blocks)
        outline = _outline(doc)

    repeated: set[str] = set()
    if page_count >= MIN_PAGES_FOR_MARGIN_REMOVAL:
        seen: Counter[str] = Counter()
        for blocks in pages:
            seen.update({_margin_key(t) for header, footer, t in blocks if header or footer})
        threshold = max(MIN_PAGES_FOR_MARGIN_REMOVAL, math.ceil(page_count * REPEAT_SHARE))
        repeated = {key for key, count in seen.items() if count >= threshold}

    sections: list[ParsedSection] = []
    empty_pages = 0
    for number, blocks in enumerate(pages, start=1):
        deadline.check()
        kept = [
            _reflow(text)
            for header, footer, text in blocks
            if not ((header or footer) and _margin_key(text) in repeated)
        ]
        text = "\n\n".join(block for block in kept if block)
        if len(re.sub(r"\s", "", text)) < MIN_CHARS_PER_PAGE:
            empty_pages += 1
        if not text:
            continue
        sections.append(ParsedSection(text=text, page=number, heading_path=outline.get(number, ())))

    if page_count and empty_pages * 2 >= page_count:
        if limits.ocr_enabled:
            raise ParseError(
                "This PDF looks scanned (no selectable text) and needs OCR, but OCR support "
                "is not available in this version."
            )
        raise NeedsOcrError(
            "This PDF looks scanned: it has no selectable text, and OCR is not enabled on this "
            "server. Upload a text-based PDF, or ask an administrator to enable OCR."
        )
    return ParsedDocument(sections=sections, page_count=page_count)
