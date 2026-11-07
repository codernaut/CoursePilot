"""Shared parser types: sections, limits, errors and text cleaning.

Document text is untrusted DATA. Parsers only extract text; nothing in a document is executed,
fetched or interpreted as an instruction.
"""

import re
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from app.core.zip_guard import ZipGuardError, precheck_zip_directory

SectionKind = Literal["body", "table", "speaker_notes", "caption"]

MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_TEXT_FILE_BYTES = 5 * 1024 * 1024  # MD/TXT/HTML parse in one uninterruptible call
MAX_TEXT_CHARS = 20_000_000  # extracted text per document
MAX_HEADING_DEPTH = 6


class ParseError(Exception):
    """The file cannot be processed. The message is written for the faculty member who uploaded
    it and is shown on the materials page, so keep it plain and actionable."""


class ParseLimitError(ParseError):
    """A parser limit (pages, size, time) was hit."""


class NeedsOcrError(ParseError):
    """A PDF with (almost) no selectable text, i.e. scanned pages."""


@dataclass(frozen=True)
class ParseLimits:
    max_pages: int = 2000
    timeout_s: float = 120.0
    max_uncompressed_bytes: int = MAX_UNCOMPRESSED_BYTES
    max_text_file_bytes: int = MAX_TEXT_FILE_BYTES
    ocr_enabled: bool = False


class Deadline:
    """Cooperative per-document time limit: parsers call ``check`` between pages/slides.

    A thread cannot be killed, so the check is how a slow or hostile file gets stopped."""

    def __init__(self, timeout_s: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._timeout = timeout_s
        self._end = clock() + timeout_s

    def check(self) -> None:
        if self._clock() > self._end:
            raise ParseLimitError(
                f"Reading this file took longer than {self._timeout:g} seconds, so it was stopped."
            )


@dataclass(frozen=True)
class ParsedSection:
    """A piece of a document in reading order. ``page`` (PDF) or ``slide_number`` (PPTX) locate it
    for citations; DOCX/MD/TXT/HTML have neither. ``heading_path`` is the chain of enclosing
    headings (outermost first); for a slide it is the slide title."""

    text: str
    page: int | None = None
    slide_number: int | None = None
    heading_path: tuple[str, ...] = ()
    kind: SectionKind = "body"


@dataclass
class ParsedDocument:
    sections: list[ParsedSection] = field(default_factory=list)
    page_count: int | None = None


_STRIP_CATEGORIES = frozenset({"Cs", "Co", "Cn", "Cf"})
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_MULTI_BLANK = re.compile(r"\n{3,}")
_TRAILING_WS = re.compile(r"[ \t]+\n")
_SPACES = re.compile(r"[ \t\N{NO-BREAK SPACE}]{2,}")


def clean_text(text: str) -> str:
    """Normalise newlines, drop control/format/surrogate characters (NUL, bidi overrides,
    zero-width) and tidy whitespace. Keeps ``\n`` and ``\t``."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\N{LINE SEPARATOR}", "\n")
    text = text.replace("\N{SOFT HYPHEN}", "")
    text = _CONTROL.sub("", text)
    if any(unicodedata.category(c) in _STRIP_CATEGORIES for c in set(text)):
        text = "".join(c for c in text if unicodedata.category(c) not in _STRIP_CATEGORIES)
    text = _SPACES.sub(" ", text)
    text = _TRAILING_WS.sub("\n", text)
    return _MULTI_BLANK.sub("\n\n", text).strip()


def clean_heading(text: str) -> str:
    """A heading as one short line."""
    return " ".join(clean_text(text).split())[:200]


def check_zip_container(data: bytes, limits: ParseLimits, label: str) -> None:
    """EOCD pre-check, then the declared decompressed size. Raises ``ParseError``."""
    import io
    import zipfile

    buffer = io.BytesIO(data)
    try:
        precheck_zip_directory(buffer)
    except ZipGuardError as exc:
        raise ParseError(f"This {label} file {exc}.") from exc
    buffer.seek(0)
    try:
        with zipfile.ZipFile(buffer) as archive:
            declared = sum(info.file_size for info in archive.infolist())
    except (zipfile.BadZipFile, NotImplementedError, OSError, EOFError, ValueError) as exc:
        raise ParseError(f"This {label} file is damaged or not a valid {label} file.") from exc
    if declared > limits.max_uncompressed_bytes:
        raise ParseLimitError(
            f"This {label} file expands to more than "
            f"{limits.max_uncompressed_bytes // (1024 * 1024)} MB, which is not allowed."
        )


def check_text_file_size(data: bytes, limits: ParseLimits, label: str) -> None:
    """MD/TXT/HTML parse in one uninterruptible call, so their size is capped up front."""
    if len(data) > limits.max_text_file_bytes:
        raise ParseLimitError(
            f"This {label} file is larger than {limits.max_text_file_bytes // (1024 * 1024)} MB "
            "of text, which is not allowed. Split it into smaller files."
        )


class TextBudget:
    """Counts extracted characters so a decompression-bomb style file cannot grow without bound."""

    def __init__(self, limit: int = MAX_TEXT_CHARS) -> None:
        self._left = limit

    def spend(self, text: str) -> None:
        self._left -= len(text)
        if self._left < 0:
            raise ParseLimitError("This file contains far more text than can be processed.")
