"""HTML parser (beautifulsoup4 with the stdlib ``html.parser``): scripts, styles and other
non-content elements are dropped, headings build the heading path, tables become their own
sections. Nothing in the page is fetched or executed."""

from typing import Any

from bs4 import BeautifulSoup, Comment, Declaration, Doctype, ProcessingInstruction
from bs4.element import NavigableString, Tag

from app.ingestion.parsers.base import (
    MAX_HEADING_DEPTH,
    Deadline,
    ParsedDocument,
    ParsedSection,
    ParseError,
    ParseLimits,
    TextBudget,
    clean_heading,
    clean_text,
)
from app.ingestion.parsers.text import decode_text

SKIP_TAGS = frozenset(
    {
        "script", "style", "noscript", "template", "svg", "math", "iframe", "object", "embed",
        "canvas", "head", "title", "meta", "link", "select", "option", "button", "input",
        "textarea",
    }
)  # fmt: skip
HEADINGS = {f"h{n}": n for n in range(1, 7)}
BLOCK_TAGS = frozenset(
    {
        "p", "div", "section", "article", "main", "header", "footer", "aside", "nav", "ul", "ol",
        "li", "dl", "dt", "dd", "blockquote", "pre", "figure", "figcaption", "form", "fieldset",
        "address", "details", "summary", "hr", "body", "html", "tr", "caption",
    }
)  # fmt: skip
MAX_DEPTH = 120
BREAK = chr(11)  # marks a <br> inside the inline buffer
_IGNORED_STRINGS = (Comment, Doctype, Declaration, ProcessingInstruction)


def parse_html(data: bytes, limits: ParseLimits) -> ParsedDocument:
    deadline = Deadline(limits.timeout_s)
    budget = TextBudget()
    try:
        soup = BeautifulSoup(decode_text(data), "html.parser")
    except RecursionError as exc:
        raise ParseError("This HTML file is nested too deeply to be read.") from exc

    sections: list[ParsedSection] = []
    stack: list[tuple[int, str]] = []
    paragraphs: list[str] = []
    inline: list[str] = []
    visited = 0

    def path() -> tuple[str, ...]:
        return tuple(title for _, title in stack)

    def flush_inline() -> None:
        lines = "".join(inline).split(BREAK)
        text = "\n".join(line for line in (" ".join(raw.split()) for raw in lines) if line)
        inline.clear()
        if text:
            paragraphs.append(text)

    def flush_section() -> None:
        flush_inline()
        text = clean_text("\n\n".join(paragraphs))
        paragraphs.clear()
        if text:
            budget.spend(text)
            sections.append(ParsedSection(text=text, heading_path=path()))

    def cell_text(cell: Tag, nested: list[Tag]) -> str:
        """Text of one cell. Iterative and linear: nested tables are queued, not read here (reading
        them per level made deeply nested tables quadratic)."""
        parts: list[str] = []
        stack: list[Any] = list(reversed(list(cell.children)))
        while stack:
            node = stack.pop()
            if isinstance(node, NavigableString):
                if not isinstance(node, _IGNORED_STRINGS):
                    parts.append(str(node))
                continue
            name = (node.name or "").lower()
            if name == "table":
                nested.append(node)
            elif name in SKIP_TAGS or node.get("hidden") is not None:
                continue
            else:
                if name in ("br", "p", "div", "li"):
                    parts.append(" ")
                stack.extend(reversed(list(node.children)))
        return " ".join("".join(parts).split())

    def table_text(table: Tag, nested: list[Tag]) -> str:
        rows: list[list[str]] = []
        stack: list[Tag] = [c for c in reversed(list(table.children)) if isinstance(c, Tag)]
        while stack:
            node = stack.pop()
            name = (node.name or "").lower()
            if name == "table":
                nested.append(node)
            elif name in ("td", "th"):
                if not rows:
                    rows.append([])
                rows[-1].append(cell_text(node, nested))
            elif name in SKIP_TAGS:
                continue
            else:
                if name == "tr":
                    rows.append([])
                stack.extend(c for c in reversed(list(node.children)) if isinstance(c, Tag))
        return chr(10).join(" | ".join(r) for r in rows if any(r))

    def emit_tables(first: Tag) -> None:
        queue = [first]
        while queue:
            deadline.check()
            nested: list[Tag] = []
            text = clean_text(table_text(queue.pop(0), nested))
            if text:
                budget.spend(text)
                sections.append(ParsedSection(text=text, heading_path=path(), kind="table"))
            queue.extend(nested)

    def walk(node: Tag, depth: int) -> None:
        nonlocal visited
        if depth > MAX_DEPTH:
            raise ParseError("This HTML file is nested too deeply to be read.")
        for child in node.children:
            visited += 1
            if visited % 2000 == 0:
                deadline.check()
            if isinstance(child, NavigableString):
                if not isinstance(child, _IGNORED_STRINGS):
                    inline.append(str(child))
                continue
            if not isinstance(child, Tag):
                continue
            name = (child.name or "").lower()
            if name in SKIP_TAGS or child.get("hidden") is not None:
                continue
            if name in HEADINGS:
                flush_section()
                level = min(HEADINGS[name], MAX_HEADING_DEPTH)
                title = clean_heading(child.get_text(" "))
                while stack and stack[-1][0] >= level:
                    stack.pop()
                if title:
                    stack.append((level, title))
            elif name == "table":
                flush_section()
                emit_tables(child)
            elif name == "br":
                inline.append(BREAK)
            elif name in BLOCK_TAGS:
                flush_inline()
                walk(child, depth + 1)
                flush_inline()
            else:
                walk(child, depth + 1)

    try:
        walk(soup, 0)
    except RecursionError as exc:
        raise ParseError("This HTML file is nested too deeply to be read.") from exc
    flush_section()
    return ParsedDocument(sections=sections, page_count=None)
