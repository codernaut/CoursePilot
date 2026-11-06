"""Tiny generated files for upload tests (all written here, no third-party material)."""

import io
import zipfile

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"


def make_zip(parts: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def make_pptx(text: str = "slide") -> bytes:
    return make_zip(
        {
            "[Content_Types].xml": b"<Types/>",
            "ppt/presentation.xml": b"<presentation/>",
            "ppt/slides/slide1.xml": text.encode(),
        }
    )


def make_docx(text: str = "hello") -> bytes:
    return make_zip(
        {
            "[Content_Types].xml": b"<Types/>",
            "word/document.xml": f"<document>{text}</document>".encode(),
        }
    )


def make_pdf(tag: str = "") -> bytes:
    return PDF_BYTES + tag.encode()


# One valid sample per allowed extension.
SAMPLES: dict[str, bytes] = {
    "pdf": make_pdf(),
    "pptx": make_pptx(),
    "docx": make_docx(),
    "md": "# Title\n\nSome *markdown* text, with unicode: café.\n".encode(),
    "txt": b"plain text\n",
    "html": b"<html><body><p>hi</p></body></html>",
}
