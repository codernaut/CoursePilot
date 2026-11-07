"""Tiny documents generated in code for the parser and pipeline tests (no third-party content)."""

import io
import struct
from collections.abc import Sequence
from typing import Any

import pymupdf
from docx import Document as DocxDocument
from pptx import Presentation
from pptx.util import Inches


def make_text_pdf(
    pages: Sequence[str],
    *,
    header: str | None = None,
    footer: str | None = None,
    toc: list[list[object]] | None = None,
) -> bytes:
    """A PDF with one body text box per page, an optional repeated header and a footer that
    ends in the page number ("Page 3"). Body text may contain explicit line breaks."""
    doc = pymupdf.open()
    for index, body in enumerate(pages, start=1):
        page = doc.new_page(width=595, height=842)
        if header:
            page.insert_text((72, 40), header, fontsize=9)
        if body:
            page.insert_textbox(pymupdf.Rect(72, 100, 523, 700), body, fontsize=11)
        if footer:
            page.insert_text((72, 810), f"{footer} {index}", fontsize=9)
    if toc:
        doc.set_toc(toc)
    data = doc.tobytes()
    doc.close()
    return bytes(data)


def make_encrypted_pdf() -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 100), "secret")
    data = doc.tobytes(
        encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="open-sesame", owner_pw="owner-pw"
    )
    doc.close()
    return bytes(data)


def make_pptx(
    slides: Sequence[dict[str, Any]],
) -> bytes:
    """Each slide dict: ``title``, ``bullets`` (list[str]), ``table`` (list[list[str]]),
    ``notes`` (str). Missing keys are skipped."""
    prs = Presentation()
    for spec in slides:
        title = spec.get("title")
        bullets = spec.get("bullets") or []
        layout = prs.slide_layouts[1] if bullets else prs.slide_layouts[5]
        slide = prs.slides.add_slide(layout)
        if slide.shapes.title is not None:
            slide.shapes.title.text = str(title or "")
        if bullets:
            body = slide.placeholders[1].text_frame
            body.text = str(bullets[0])
            for line in bullets[1:]:
                body.add_paragraph().text = str(line)
        table = spec.get("table")
        if table:
            rows, cols = len(table), len(table[0])
            shape = slide.shapes.add_table(rows, cols, Inches(1), Inches(4), Inches(6), Inches(1.5))
            for r, row in enumerate(table):
                for c, value in enumerate(row):
                    shape.table.cell(r, c).text = str(value)
        notes = spec.get("notes")
        if notes:
            slide.notes_slide.notes_text_frame.text = str(notes)
    out = io.BytesIO()
    prs.save(out)
    return out.getvalue()


def make_docx(blocks: Sequence[tuple[str, Any]]) -> bytes:
    """Blocks: ("h1", text) .. ("h3", text), ("p", text), ("table", [[cell, ...], ...])."""
    doc = DocxDocument()
    for kind, value in blocks:
        if kind.startswith("h"):
            doc.add_heading(str(value), level=int(kind[1:]))
        elif kind == "p":
            doc.add_paragraph(str(value))
        elif kind == "table":
            rows = value
            table = doc.add_table(rows=len(rows), cols=len(rows[0]))
            for r, row in enumerate(rows):
                for c, cell in enumerate(row):
                    table.cell(r, c).text = str(cell)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def forge_eocd(data: bytes, *, entries: int | None = None, cd_size: int | None = None) -> bytes:
    """Rewrite the end-of-central-directory record of a (comment-free) zip to hostile values."""
    pos = data.rfind(b"PK\x05\x06")
    assert pos >= 0
    head = data[:pos]
    sig, disk, cd_disk, here, total, size, offset, comment = struct.unpack(
        "<4s4H2LH", data[pos : pos + 22]
    )
    if entries is not None:
        here = total = entries
    if cd_size is not None:
        size = cd_size
    return head + struct.pack("<4s4H2LH", sig, disk, cd_disk, here, total, size, offset, comment)


def add_zip64_eocd(data: bytes, *, entries: int, cd_size: int) -> bytes:
    """Replace the EOCD with a ZIP64 EOCD record + locator + a classic EOCD marked 0xFFFF."""
    pos = data.rfind(b"PK\x05\x06")
    head = data[:pos]
    _sig, _disk, _cd_disk, _here, _total, _size, offset, _comment = struct.unpack(
        "<4s4H2LH", data[pos : pos + 22]
    )
    record_offset = len(head)
    record = struct.pack(
        "<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, entries, entries, cd_size, offset
    )
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, record_offset, 1)
    eocd = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0)
    return head + record + locator + eocd
