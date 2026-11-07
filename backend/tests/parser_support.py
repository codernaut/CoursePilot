"""Module-level parse functions for the parser-pool tests (the child imports them by name)."""

import os
import time

from app.ingestion.parsers import ParsedDocument, ParsedSection, ParseError, ParseLimits


def ok_parse(extension: str, data: bytes, limits: ParseLimits) -> ParsedDocument:
    return ParsedDocument(
        sections=[ParsedSection(text=data.decode(), heading_path=("T",))], page_count=1
    )


def slow_parse(extension: str, data: bytes, limits: ParseLimits) -> ParsedDocument:
    """A parser that never returns (a pathological file stuck inside one library call)."""
    while True:
        time.sleep(0.05)


def crash_parse(extension: str, data: bytes, limits: ParseLimits) -> ParsedDocument:
    os._exit(3)  # a hard crash of the child (out-of-memory kill, segfault in an extension)


def failing_parse(extension: str, data: bytes, limits: ParseLimits) -> ParsedDocument:
    raise ParseError("This file is broken in a readable way.")


def pid_parse(extension: str, data: bytes, limits: ParseLimits) -> ParsedDocument:
    return ParsedDocument(sections=[ParsedSection(text=str(os.getpid()))], page_count=1)
