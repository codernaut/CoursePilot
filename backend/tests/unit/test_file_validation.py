import io

import pytest

from app.services.file_validation import (
    ALLOWED_EXTENSIONS,
    ALLOWED_TYPES,
    MAX_FILENAME_LENGTH,
    ContentChecker,
    ContentError,
    check_zip,
    file_extension,
    sanitize_filename,
    title_from_filename,
)
from tests.files import SAMPLES, make_docx, make_pptx, make_zip


def test_allow_list_matches_the_spec() -> None:
    assert set(ALLOWED_EXTENSIONS) == {"pdf", "pptx", "docx", "md", "txt", "html"}
    assert ALLOWED_TYPES["pdf"].mime_type == "application/pdf"
    assert ALLOWED_TYPES["html"].mime_type == "text/html"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("notes.pdf", "notes.pdf"),
        ("../../etc/passwd.txt", "passwd.txt"),
        ("..\\..\\windows\\system32\\a.txt", "a.txt"),
        ("C:\\fakepath\\slides.pptx", "slides.pptx"),
        ("a/b/c.md", "c.md"),
        ("bad\x00name\x1b.pdf", "badname.pdf"),
        ("tab\tnew\nline.txt", "tab new line.txt"),
        ("  spaced   out  .pdf  ", "spaced out .pdf"),
        ("trailing dots...", "trailing dots"),
        ("...", "unnamed"),
        ("", "unnamed"),
        ("/", "unnamed"),
        ("r\u202etxt.exe", "rtxt.exe"),  # right-to-left override removed
        ("zero\u200bwidth.pdf", "zerowidth.pdf"),
        ("r\u00e9sum\u00e9 \u8bb2\u4e49.pdf", "r\u00e9sum\u00e9 \u8bb2\u4e49.pdf"),
        ("e\u0301.pdf", "\u00e9.pdf"),  # NFC normalised
        ('quote".pdf', 'quote".pdf'),
    ],
)
def test_sanitize_filename(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_truncates_but_keeps_the_extension() -> None:
    name = sanitize_filename("x" * 1000 + ".pdf")
    assert len(name) == MAX_FILENAME_LENGTH
    assert name.endswith(".pdf")
    assert len(sanitize_filename("y" * 1000)) == MAX_FILENAME_LENGTH
    # An absurdly long "extension" is cut like the rest of the name.
    assert len(sanitize_filename("a." + "z" * 500)) <= MAX_FILENAME_LENGTH


def test_extension_and_title() -> None:
    assert file_extension("Lecture.1.PDF") == "pdf"
    assert file_extension("noext") == ""
    assert file_extension(".pdf") == ""
    assert title_from_filename("Lecture 1.pdf") == "Lecture 1"
    assert title_from_filename("archive.tar.gz") == "archive.tar"


def feed_all(extension: str, data: bytes, chunk: int = 3) -> None:
    checker = ContentChecker(ALLOWED_TYPES[extension])
    for i in range(0, len(data), chunk):
        checker.feed(data[i : i + chunk])
    checker.finish()


@pytest.mark.parametrize("extension", ALLOWED_EXTENSIONS)
def test_samples_pass_streaming_checks_in_tiny_chunks(extension: str) -> None:
    feed_all(extension, SAMPLES[extension], chunk=1)
    feed_all(extension, SAMPLES[extension], chunk=4096)


@pytest.mark.parametrize(
    ("extension", "data"),
    [
        ("pdf", b"PK\x03\x04 not a pdf"),
        ("pdf", b"hello"),
        ("pdf", b"%PD"),  # shorter than the magic
        ("pdf", b" %PDF-1.4"),  # must START with the magic
        ("pptx", b"%PDF-1.4"),
        ("docx", b"plain text"),
        ("txt", b"abc\x00def"),
        ("md", b"\xff\xfe invalid utf-8"),
        ("html", b"<p>caf\xc3</p>"),  # truncated multi-byte sequence
    ],
)
def test_content_mismatch_is_rejected(extension: str, data: bytes) -> None:
    with pytest.raises(ContentError):
        feed_all(extension, data, chunk=2)


def test_multibyte_character_split_across_chunks_is_fine() -> None:
    text = "caf\u00e9 \u8bb2\u4e49".encode()
    for size in (1, 2, 3):
        feed_all("txt", text, chunk=size)


def test_check_zip_accepts_real_structures() -> None:
    check_zip(io.BytesIO(make_pptx()), ALLOWED_TYPES["pptx"])
    check_zip(io.BytesIO(make_docx()), ALLOWED_TYPES["docx"])


def test_check_zip_rejects_missing_parts_and_wrong_kind() -> None:
    with pytest.raises(ContentError, match=r"word/document.xml"):
        check_zip(io.BytesIO(make_zip({"[Content_Types].xml": b"x"})), ALLOWED_TYPES["docx"])
    with pytest.raises(ContentError):
        check_zip(io.BytesIO(make_docx()), ALLOWED_TYPES["pptx"])
    with pytest.raises(ContentError):
        check_zip(io.BytesIO(b"PK\x03\x04 garbage that is not a zip"), ALLOWED_TYPES["docx"])


def test_check_zip_rejects_zip_bombs_and_unsafe_names() -> None:
    bomb = make_zip(
        {
            "[Content_Types].xml": b"<Types/>",
            "word/document.xml": b"<d/>",
            "word/big.bin": b"\x00" * (3 * 1024 * 1024),
        }
    )
    assert len(bomb) < 20_000  # tiny on disk
    with pytest.raises(ContentError, match="zip bomb"):
        check_zip(io.BytesIO(bomb), ALLOWED_TYPES["docx"], max_uncompressed=1024 * 1024)
    check_zip(io.BytesIO(bomb), ALLOWED_TYPES["docx"], max_uncompressed=10 * 1024 * 1024)
    evil = make_zip({"[Content_Types].xml": b"x", "word/document.xml": b"x", "../evil": b"x"})
    with pytest.raises(ContentError, match="unsafe"):
        check_zip(io.BytesIO(evil), ALLOWED_TYPES["docx"])
