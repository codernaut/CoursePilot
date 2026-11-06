"""Upload validation: filename sanitising and content checks beyond the extension (CLAUDE.md 13).

The extension must be on the allow-list AND the content must look like that type: PDFs start with
``%PDF-``, PPTX/DOCX are zips with the expected parts and a capped decompressed size, and
MD/TXT/HTML are UTF-8 without NUL bytes. Nothing here imports FastAPI.
"""

import asyncio
import codecs
import re
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import PurePath
from typing import IO

from app.storage import Storage

# Total declared decompressed size allowed for a PPTX/DOCX. zipfile never yields more than the
# declared size per member, so the declaration is a hard upper bound for any reader.
MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024
MAX_ZIP_ENTRIES = 10_000
MAX_FILENAME_LENGTH = 200
_MAX_EXTENSION_LENGTH = 16
_SPOOL_BYTES = 8 * 1024 * 1024

ZIP_MAGIC = b"PK\x03\x04"
PDF_MAGIC = b"%PDF-"

_DRIVE_LETTER = re.compile(r"^[A-Za-z]:")
_REMOVED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


@dataclass(frozen=True)
class FileType:
    extension: str
    mime_type: str
    kind: str  # "pdf" | "pptx" | "docx" | "text"
    required_parts: tuple[str, ...] = ()


ALLOWED_TYPES: dict[str, FileType] = {
    t.extension: t
    for t in (
        FileType("pdf", "application/pdf", "pdf"),
        FileType(
            "pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "pptx",
            ("[Content_Types].xml", "ppt/presentation.xml"),
        ),
        FileType(
            "docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "docx",
            ("[Content_Types].xml", "word/document.xml"),
        ),
        FileType("md", "text/markdown", "text"),
        FileType("txt", "text/plain", "text"),
        FileType("html", "text/html", "text"),
    )
}
ALLOWED_EXTENSIONS = tuple(ALLOWED_TYPES)


class ContentError(Exception):
    """The content does not match the declared file type."""


def sanitize_filename(raw: str) -> str:
    """A display-safe filename: no path parts, control/format characters or odd whitespace.

    The result is for display and download headers only; it is never used as a path.
    """
    name = unicodedata.normalize("NFC", raw)
    name = re.split(r"[\\/]", name)[-1]
    name = "".join(" " if c.isspace() else c for c in name)
    name = "".join(c for c in name if unicodedata.category(c) not in _REMOVED_CATEGORIES)
    name = re.sub(r"\s+", " ", name).strip().strip(".").strip()
    if len(name) > MAX_FILENAME_LENGTH:
        suffix = PurePath(name).suffix
        if len(suffix) > _MAX_EXTENSION_LENGTH:
            suffix = ""
        name = name[: MAX_FILENAME_LENGTH - len(suffix)].rstrip() + suffix
    return name or "unnamed"


def strip_unsafe_characters(text: str) -> str:
    """Drop control, format (bidi overrides, zero-width) and unassigned characters."""
    return "".join(c for c in text if unicodedata.category(c) not in _REMOVED_CATEGORIES)


def file_extension(filename: str) -> str:
    return PurePath(filename).suffix.lower().lstrip(".")


def title_from_filename(filename: str) -> str:
    stem = PurePath(filename).stem.strip()
    return (stem or filename)[:300]


class ContentChecker:
    """Incremental content check applied while the upload streams."""

    def __init__(self, file_type: FileType) -> None:
        self._type = file_type
        self._head = b""
        self._magic_checked = False
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")

    def _check_magic(self, final: bool) -> None:
        if self._magic_checked:
            return
        magic = {"pdf": PDF_MAGIC, "pptx": ZIP_MAGIC, "docx": ZIP_MAGIC}.get(self._type.kind)
        if magic is None:
            self._magic_checked = True
            return
        if len(self._head) >= len(magic) or final:
            self._magic_checked = True
            if not self._head.startswith(magic):
                raise ContentError(f"The content is not a valid {self._type.extension.upper()}.")

    def feed(self, data: bytes) -> None:
        if not self._magic_checked:
            self._head = (self._head + data)[: len(ZIP_MAGIC) + len(PDF_MAGIC)]
            self._check_magic(final=False)
        if self._type.kind == "text":
            if b"\x00" in data:
                raise ContentError("Text files must not contain NUL bytes.")
            try:
                self._decoder.decode(data, final=False)
            except UnicodeDecodeError:
                raise ContentError("Text files must be valid UTF-8.") from None

    def finish(self) -> None:
        self._check_magic(final=True)
        if self._type.kind == "text":
            try:
                self._decoder.decode(b"", final=True)
            except UnicodeDecodeError:
                raise ContentError("Text files must be valid UTF-8.") from None


def check_zip(file: IO[bytes], file_type: FileType, *, max_uncompressed: int | None = None) -> None:
    """Validate the zip structure of a PPTX/DOCX (blocking; run in a thread)."""
    limit = MAX_UNCOMPRESSED_BYTES if max_uncompressed is None else max_uncompressed
    label = file_type.extension.upper()
    try:
        with zipfile.ZipFile(file) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                raise ContentError(f"The {label} contains too many parts.")
            names = {info.filename for info in infos}
            if any(
                n.startswith(("/", "\\")) or _DRIVE_LETTER.match(n) or ".." in re.split(r"[\\/]", n)
                for n in names
            ):
                raise ContentError(f"The {label} contains unsafe part names.")
            if sum(info.file_size for info in infos) > limit:
                raise ContentError(
                    f"The {label} expands to more than {limit // (1024 * 1024)} MB; "
                    "it looks like a zip bomb."
                )
            missing = [p for p in file_type.required_parts if p not in names]
            if missing:
                raise ContentError(f"The content is not a valid {label} (missing {missing[0]}).")
    except (zipfile.BadZipFile, NotImplementedError, OSError, EOFError, ValueError) as exc:
        raise ContentError(f"The content is not a valid {label}.") from exc


async def check_stored_zip(
    storage: Storage, key: str, file_type: FileType, *, max_uncompressed: int | None = None
) -> None:
    """Read the stored object back into a temp file and validate its zip structure."""
    with tempfile.SpooledTemporaryFile(max_size=_SPOOL_BYTES) as tmp:
        async for chunk in storage.read(key):
            await asyncio.to_thread(tmp.write, chunk)
        tmp.seek(0)
        await asyncio.to_thread(check_zip, tmp, file_type, max_uncompressed=max_uncompressed)
