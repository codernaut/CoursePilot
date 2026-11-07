"""PPTX parser (python-pptx): one body section per slide (title as the heading), tables as their
own sections and speaker notes as their own sections."""

import io
from collections.abc import Iterator
from typing import Any

from app.ingestion.parsers.base import (
    Deadline,
    ParsedDocument,
    ParsedSection,
    ParseError,
    ParseLimitError,
    ParseLimits,
    TextBudget,
    check_zip_container,
    clean_heading,
    clean_text,
)

MAX_SHAPE_DEPTH = 20


def _is_group(shape: Any) -> bool:
    try:
        return bool(shape.shape_type == 6 and hasattr(shape, "shapes"))  # MSO_SHAPE_TYPE.GROUP
    except NotImplementedError:  # python-pptx cannot classify some exotic shapes
        return False


def _walk_shapes(shapes: Any, depth: int = 0) -> Iterator[Any]:
    """Shapes in reading order (top, then left), descending into groups."""
    if depth > MAX_SHAPE_DEPTH:
        return
    ordered = sorted(shapes, key=lambda s: (s.top or 0, s.left or 0))
    for shape in ordered:
        if _is_group(shape):
            yield from _walk_shapes(shape.shapes, depth + 1)
        else:
            yield shape


def _table_text(table: Any) -> str:
    rows = []
    for row in table.rows:
        cells = [" ".join(clean_text(cell.text).split()) for cell in row.cells]
        if any(cells):
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def parse_pptx(data: bytes, limits: ParseLimits) -> ParsedDocument:
    from pptx import Presentation

    check_zip_container(data, limits, "PPTX")
    deadline = Deadline(limits.timeout_s)
    budget = TextBudget()
    try:
        presentation = Presentation(io.BytesIO(data))
        slides = list(presentation.slides)
    except Exception as exc:
        raise ParseError("This PPTX file is damaged or cannot be opened.") from exc
    if len(slides) > limits.max_pages:
        raise ParseLimitError(
            f"This presentation has {len(slides)} slides; the limit is {limits.max_pages}."
        )

    sections: list[ParsedSection] = []
    for number, slide in enumerate(slides, start=1):
        deadline.check()
        try:
            title_shape = slide.shapes.title
            title = clean_heading(title_shape.text_frame.text) if title_shape is not None else ""
            heading = (title,) if title else ()
            body: list[str] = []
            tables: list[str] = []
            for shape in _walk_shapes(slide.shapes):
                if title_shape is not None and shape.shape_id == title_shape.shape_id:
                    continue
                if getattr(shape, "has_table", False) and shape.has_table:
                    text = _table_text(shape.table)
                    if text:
                        tables.append(text)
                elif getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                    lines = []
                    for paragraph in shape.text_frame.paragraphs:
                        line = clean_text(paragraph.text.replace(chr(11), chr(10)))
                        if line:
                            lines.append(("  " * min(paragraph.level, 4)) + line)
                    if lines:
                        body.append("\n".join(lines))
            notes = ""
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
                notes = clean_text(slide.notes_slide.notes_text_frame.text)
        except Exception as exc:
            raise ParseError(f"Slide {number} of this presentation could not be read.") from exc

        body_text = clean_text("\n\n".join(body))
        if not body_text and title:
            body_text = title  # a title-only slide still carries searchable text
        for text, kind in ((body_text, "body"), ("\n\n".join(tables), "table")):
            if text:
                budget.spend(text)
                sections.append(
                    ParsedSection(
                        text=text,
                        slide_number=number,
                        heading_path=heading,
                        kind=kind,  # type: ignore[arg-type]
                    )
                )
        if notes:
            budget.spend(notes)
            sections.append(
                ParsedSection(
                    text=notes,
                    slide_number=number,
                    heading_path=heading,
                    kind="speaker_notes",
                )
            )
    return ParsedDocument(sections=sections, page_count=len(slides))
