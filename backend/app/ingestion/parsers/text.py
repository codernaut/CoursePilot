"""Plain text and Markdown parsers."""

from markdown_it import MarkdownIt
from markdown_it.token import Token

from app.ingestion.parsers.base import (
    MAX_HEADING_DEPTH,
    Deadline,
    ParsedDocument,
    ParsedSection,
    ParseLimits,
    SectionKind,
    TextBudget,
    clean_heading,
    clean_text,
)


def decode_text(data: bytes) -> str:
    """Uploads are validated as UTF-8; stay tolerant anyway (BOM stripped, bad bytes replaced)."""
    return data.decode("utf-8-sig", errors="replace")


def parse_txt(data: bytes, limits: ParseLimits) -> ParsedDocument:
    text = clean_text(decode_text(data))
    TextBudget().spend(text)
    sections = [ParsedSection(text=text)] if text else []
    return ParsedDocument(sections=sections, page_count=None)


def _inline_text(token: Token) -> str:
    """Plain text of an inline token: markup characters dropped, images give their alt text."""
    parts: list[str] = []
    for child in token.children or []:
        if child.type in ("text", "code_inline"):
            parts.append(child.content)
        elif child.type == "softbreak":
            parts.append(" ")
        elif child.type == "hardbreak":
            parts.append("\n")
        elif child.type == "image":
            parts.append(child.content)
    return "".join(parts)


def parse_markdown(data: bytes, limits: ParseLimits) -> ParsedDocument:
    deadline = Deadline(limits.timeout_s)
    budget = TextBudget()
    # Raw HTML in Markdown becomes html_block / html_inline tokens, which we ignore.
    md = MarkdownIt("commonmark").enable("table")
    tokens = md.parse(decode_text(data))

    sections: list[ParsedSection] = []
    stack: list[tuple[int, str]] = []
    blocks: list[str] = []

    def path() -> tuple[str, ...]:
        return tuple(title for _, title in stack)

    def emit(text: str, kind: SectionKind = "body") -> None:
        text = clean_text(text)
        if text:
            budget.spend(text)
            sections.append(ParsedSection(text=text, heading_path=path(), kind=kind))

    def flush() -> None:
        emit("\n\n".join(blocks))
        blocks.clear()

    i = 0
    list_depth = 0
    row: list[str] = []
    table_rows: list[str] = []
    while i < len(tokens):
        if i % 500 == 0:
            deadline.check()
        token = tokens[i]
        kind = token.type
        if kind == "heading_open":
            flush()
            level = min(int(token.tag[1:]), MAX_HEADING_DEPTH)
            title = clean_heading(_inline_text(tokens[i + 1]))
            while stack and stack[-1][0] >= level:
                stack.pop()
            if title:
                stack.append((level, title))
            i += 3  # heading_open, inline, heading_close
            continue
        if kind in ("bullet_list_open", "ordered_list_open"):
            list_depth += 1
        elif kind in ("bullet_list_close", "ordered_list_close"):
            list_depth = max(0, list_depth - 1)
        elif kind == "inline":
            text = _inline_text(token)
            previous = tokens[i - 1].type if i else ""
            if previous in ("th_open", "td_open"):
                row.append(" ".join(text.split()))
            elif list_depth and previous == "paragraph_open":
                blocks.append(("  " * (list_depth - 1)) + "- " + text)
            elif text:
                blocks.append(text)
        elif kind in ("fence", "code_block"):
            blocks.append(token.content.rstrip("\n"))
        elif kind == "tr_close":
            if any(row):
                table_rows.append(" | ".join(row))
            row = []
        elif kind == "table_open":
            flush()
            table_rows = []
        elif kind == "table_close":
            emit("\n".join(table_rows), "table")
            table_rows = []
        i += 1
    flush()
    return ParsedDocument(sections=sections, page_count=None)
