"""One parser module per file type; each turns bytes into ``ParsedSection`` objects."""

from collections.abc import Callable

from app.ingestion.parsers.base import (
    NeedsOcrError,
    ParsedDocument,
    ParsedSection,
    ParseError,
    ParseLimitError,
    ParseLimits,
    SectionKind,
)
from app.ingestion.parsers.docx import parse_docx
from app.ingestion.parsers.html import parse_html
from app.ingestion.parsers.pdf import parse_pdf
from app.ingestion.parsers.pptx import parse_pptx
from app.ingestion.parsers.text import parse_markdown, parse_txt

PARSERS: dict[str, Callable[[bytes, ParseLimits], ParsedDocument]] = {
    "pdf": parse_pdf,
    "pptx": parse_pptx,
    "docx": parse_docx,
    "md": parse_markdown,
    "txt": parse_txt,
    "html": parse_html,
}


def parse_document(extension: str, data: bytes, limits: ParseLimits) -> ParsedDocument:
    """Parse ``data`` with the parser for ``extension`` (blocking; run it in a thread)."""
    parser = PARSERS.get(extension.lower().lstrip("."))
    if parser is None:
        raise ParseError(f"Files of type .{extension} cannot be processed.")
    return parser(data, limits)


__all__ = [
    "PARSERS",
    "NeedsOcrError",
    "ParseError",
    "ParseLimitError",
    "ParseLimits",
    "ParsedDocument",
    "ParsedSection",
    "SectionKind",
    "parse_document",
]
