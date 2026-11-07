"""DOCX parser (python-docx): heading styles build the heading path, body paragraphs between
headings form one section, tables become their own sections. Page numbers are unknown (a DOCX has
no fixed pagination), so sections carry no page."""

import io
import re
from typing import Any

from app.ingestion.parsers.base import (
    MAX_HEADING_DEPTH,
    Deadline,
    ParsedDocument,
    ParsedSection,
    ParseError,
    ParseLimits,
    TextBudget,
    check_zip_container,
    clean_heading,
    clean_text,
)

_HEADING_STYLE = re.compile(r"^heading\s*(\d+)$", re.IGNORECASE)


def _heading_level(paragraph: Any) -> int | None:
    try:
        style = paragraph.style
        name = (style.name if style is not None else "") or ""
    except Exception:
        return None
    if name.strip().lower() == "title":
        return 1
    match = _HEADING_STYLE.match(name.strip())
    if match:
        return max(1, min(int(match.group(1)), MAX_HEADING_DEPTH))
    return None


def _table_text(table: Any) -> str:
    rows: list[str] = []
    for row in table.rows:
        cells: list[str] = []
        previous = None
        for cell in row.cells:
            if cell._tc is previous:  # merged cells repeat the same cell object
                continue
            previous = cell._tc
            cells.append(" ".join(clean_text(cell.text).split()))
        if any(cells):
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def parse_docx(data: bytes, limits: ParseLimits) -> ParsedDocument:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    check_zip_container(data, limits, "DOCX")
    deadline = Deadline(limits.timeout_s)
    budget = TextBudget()
    try:
        document = Document(io.BytesIO(data))
        items = list(document.iter_inner_content())
    except Exception as exc:
        raise ParseError("This DOCX file is damaged or cannot be opened.") from exc

    sections: list[ParsedSection] = []
    stack: list[tuple[int, str]] = []
    paragraphs: list[str] = []

    def path() -> tuple[str, ...]:
        return tuple(title for _, title in stack)

    def flush() -> None:
        text = clean_text("\n\n".join(paragraphs))
        paragraphs.clear()
        if text:
            budget.spend(text)
            sections.append(ParsedSection(text=text, heading_path=path()))

    for count, item in enumerate(items):
        if count % 200 == 0:
            deadline.check()
        if isinstance(item, Paragraph):
            level = _heading_level(item)
            text = clean_text(item.text)
            if level is not None and text:
                flush()
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, clean_heading(text)))
            elif text:
                paragraphs.append(text)
        elif isinstance(item, Table):
            flush()
            text = _table_text(item)
            if text:
                budget.spend(text)
                sections.append(ParsedSection(text=text, heading_path=path(), kind="table"))
    flush()
    return ParsedDocument(sections=sections, page_count=None)
