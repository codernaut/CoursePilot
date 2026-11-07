"""Structure-first chunking (CLAUDE.md section 7.2).

1. Sections are grouped by structure: a slide (its body and tables) is one unit, speaker notes and
   tables are units of their own, and consecutive body sections under the same heading path form
   one flow of text.
2. Slides that fit are one chunk; very small consecutive slides under the same parent heading are
   merged. Flows and oversized slides are packed by size: about ``target_tokens`` per chunk, never
   more than ``max_tokens``, with an ``overlap_min``-``overlap_max`` token overlap made of whole
   sentences (so a chunk never starts or ends in the middle of a sentence). The one exception is a
   single "sentence" longer than ``max_tokens`` (no punctuation at all): it is split at word
   boundaries and the chunk is marked ``hard_split`` in its metadata.
3. The embedded text is the raw text prefixed with ``"{programme path} | {document title} |
   {section path}"``; the raw text is stored separately for display.

Page tracking: every sentence remembers its page, and a chunk's ``page_start``/``page_end`` are the
smallest and largest page among its sentences (overlap included), so text is never attributed to a
page it did not come from.
"""

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from app.ingestion.parsers.base import ParsedSection
from app.ingestion.tokens import COUNTER_NAME, count_tokens

CHUNKER_VERSION = 1


@dataclass(frozen=True)
class ChunkParams:
    max_tokens: int = 800
    target_tokens: int = 650
    overlap_min_tokens: int = 80
    overlap_max_tokens: int = 120
    merge_slides_below_tokens: int = 150
    min_tail_tokens: int = 120

    def as_metadata(self) -> dict[str, Any]:
        return {"version": CHUNKER_VERSION, "token_counter": COUNTER_NAME, **asdict(self)}


DEFAULT_PARAMS = ChunkParams()


@dataclass
class DocChunk:
    ordinal: int
    text: str
    embed_text: str
    token_count: int
    page_start: int | None
    page_end: int | None
    slide_number: int | None
    section_path: str | None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _Atom:
    """An unsplittable piece of text (a sentence, or a table row) and what follows it."""

    text: str
    sep: str
    page: int | None
    tokens: int
    hard: bool = False


@dataclass
class _Unit:
    """Something to chunk: a flow of text, a table, a slide or notes."""

    kind: str  # body | table | speaker_notes | slide
    heading_path: tuple[str, ...]
    sections: list[ParsedSection]
    slide_number: int | None = None


# -- sentence splitting -------------------------------------------------------------------------

_ABBREVIATIONS = frozenset(
    [
        "e.g",
        "i.e",
        "etc",
        "vs",
        "fig",
        "figs",
        "eq",
        "eqs",
        "no",
        "nos",
        "dr",
        "mr",
        "mrs",
        "ms",
        "prof",
        "sr",
        "jr",
        "st",
        "cf",
        "approx",
        "dept",
        "univ",
        "al",
        "vol",
        "ch",
        "sec",
        "pp",
        "p",
        "inc",
        "ltd",
        "co",
    ]
)
_BOUNDARY = re.compile(r"""(?:([.!?…]+["')\]»”’]*)(\s+))|(?:([。！？]+[」』）]*)(\s*))""")
_LIST_START = re.compile(r"^\s*(?:[-*•–]\s|\d{1,3}[.)]\s|\(?[a-zA-Z]\)\s)")
_LAST_WORD = re.compile(r"([^\W_]+)\W*$")
_LOOKBACK = 24  # characters scanned for the word before a dot; longer words are never abbreviations


def split_sentences(text: str) -> list[str]:
    """Split one paragraph into sentences. Errs on the side of NOT splitting."""
    sentences: list[str] = []
    start = 0
    for match in _BOUNDARY.finditer(text):
        end = match.end(1) if match.group(1) else match.end(3)
        following = text[match.end() : match.end() + 1]
        if match.group(1):
            if not following or following.islower():
                continue
            if match.group(1).startswith(".") and not match.group(1).startswith(".."):
                # Bounded look-back: scanning back to ``start`` made text full of skipped
                # boundaries ("A. B. C. ...") quadratic.
                window_start = max(start, match.start(1) - _LOOKBACK)
                word = _LAST_WORD.search(text, window_start, match.start(1))
                if word and (word.start(1) > window_start or window_start == start):
                    token = word.group(1).lower()
                    if token in _ABBREVIATIONS or (len(token) == 1 and token.isalpha()):
                        continue
        elif not following:
            continue
        piece = text[start:end].strip()
        if piece:
            sentences.append(piece)
        start = match.end()
    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def _paragraph_lines(paragraph: str) -> list[str]:
    """Lines of a paragraph, with soft-wrapped lines (no sentence end, no list marker) rejoined."""
    lines: list[str] = []
    for raw in paragraph.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            continue
        indented = raw.startswith((" ", "\t"))
        wrapped = (
            bool(lines) and not _LIST_START.match(line) and not re.search(r"[.!?:;…]$", lines[-1])
        )
        if wrapped and not indented:
            lines[-1] = lines[-1] + " " + line.strip()
            continue
        lines.append(line if indented else line.strip())
    return lines


def _slice_word(word: str, max_tokens: int) -> list[str]:
    """Cut a word with no break opportunity into slices of at most ``max_tokens`` tokens (CJK
    characters cost a token each, other text about a token per four characters)."""
    pieces: list[str] = []
    position = 0
    while position < len(word):
        size = min(len(word) - position, max(1, max_tokens * 4 - 1))
        while size > 1 and count_tokens(word[position : position + size]) > max_tokens:
            size = max(
                1,
                min(size - 1, size * max_tokens // count_tokens(word[position : position + size])),
            )
        pieces.append(word[position : position + size])
        position += size
    return pieces


def _hard_split(text: str, max_tokens: int) -> list[str]:
    """Last resort for a 'sentence' longer than ``max_tokens``: split at whitespace."""
    pieces: list[str] = []
    current: list[str] = []
    used = 0
    for word in text.split():
        cost = count_tokens(word)
        if cost > max_tokens:  # a single huge token: slice it
            if current:
                pieces.append(" ".join(current))
                current, used = [], 0
            pieces.extend(_slice_word(word, max_tokens))
            continue
        if used + cost > max_tokens and current:
            pieces.append(" ".join(current))
            current, used = [], 0
        current.append(word)
        used += cost
    if current:
        pieces.append(" ".join(current))
    return pieces


def _atoms_from_sections(
    sections: list[ParsedSection], params: ChunkParams, *, by_row: bool
) -> list[_Atom]:
    atoms: list[_Atom] = []
    for section in sections:
        paragraphs = [p for p in re.split(r"\n{2,}", section.text) if p.strip()]
        for paragraph in paragraphs:
            if by_row:
                lines = [line.strip() for line in paragraph.split("\n") if line.strip()]
                pieces_per_line = [[line] for line in lines]
            else:
                lines = _paragraph_lines(paragraph)
                pieces_per_line = [split_sentences(line) or [line] for line in lines]
            for l_index, pieces in enumerate(pieces_per_line):
                last_line = l_index == len(pieces_per_line) - 1
                for s_index, sentence in enumerate(pieces):
                    last_sentence = s_index == len(pieces) - 1
                    if not last_sentence:
                        sep = " "
                    elif not last_line:
                        sep = "\n"
                    else:
                        sep = "\n\n"
                    tokens = count_tokens(sentence)
                    if tokens > params.max_tokens:
                        parts = _hard_split(sentence, params.max_tokens)
                        for i, part in enumerate(parts):
                            atoms.append(
                                _Atom(
                                    part,
                                    sep if i == len(parts) - 1 else " ",
                                    section.page,
                                    count_tokens(part),
                                    hard=True,
                                )
                            )
                    else:
                        atoms.append(_Atom(sentence, sep, section.page, tokens))
    return atoms


# -- packing ------------------------------------------------------------------------------------


@dataclass
class _Piece:
    atoms: list[_Atom]
    new_from: int  # index of the first atom that is not overlap

    @property
    def tokens(self) -> int:
        return sum(a.tokens for a in self.atoms)

    @property
    def new_tokens(self) -> int:
        return sum(a.tokens for a in self.atoms[self.new_from :])


def _overlap_tail(atoms: list[_Atom], params: ChunkParams) -> list[_Atom]:
    tail: list[_Atom] = []
    total = 0
    for atom in reversed(atoms):
        if len(tail) >= len(atoms) - 1 and len(atoms) > 1:
            break  # never repeat the whole chunk
        if total + atom.tokens > params.overlap_max_tokens:
            break
        tail.insert(0, atom)
        total += atom.tokens
        if total >= params.overlap_min_tokens:
            break
    return tail if len(tail) < len(atoms) else []


def _pack(atoms: list[_Atom], params: ChunkParams, *, overlap: bool = True) -> list[_Piece]:
    pieces: list[_Piece] = []
    carry: list[_Atom] = []
    i = 0
    while i < len(atoms):
        if carry and sum(a.tokens for a in carry) + atoms[i].tokens > params.max_tokens:
            carry = []
        current = list(carry)
        tokens = sum(a.tokens for a in current)
        new_from = len(current)
        while i < len(atoms):
            atom = atoms[i]
            if len(current) > new_from and tokens + atom.tokens > params.max_tokens:
                break
            current.append(atom)
            tokens += atom.tokens
            i += 1
            if tokens >= params.target_tokens:
                break
        pieces.append(_Piece(current, new_from))
        carry = _overlap_tail(current, params) if overlap and i < len(atoms) else []
    if len(pieces) > 1:
        last, previous = pieces[-1], pieces[-2]
        if last.new_tokens < params.min_tail_tokens:
            folded = previous.tokens + last.new_tokens
            if folded <= params.max_tokens:
                previous.atoms = previous.atoms + last.atoms[last.new_from :]
                pieces.pop()
    return pieces


def _join(atoms: list[_Atom]) -> str:
    text = "".join(a.text + a.sep for a in atoms[:-1]) + atoms[-1].text
    return text.strip()


def _page_range(atoms: list[_Atom]) -> tuple[int | None, int | None]:
    pages = [a.page for a in atoms if a.page is not None]
    return (min(pages), max(pages)) if pages else (None, None)


# -- grouping -----------------------------------------------------------------------------------


def _group(sections: list[ParsedSection]) -> list[_Unit]:
    units: list[_Unit] = []
    for section in sections:
        if not section.text.strip():
            continue
        if section.slide_number is not None:
            if section.kind == "speaker_notes":
                units.append(
                    _Unit("speaker_notes", section.heading_path, [section], section.slide_number)
                )
                continue
            last = units[-1] if units else None
            if last and last.kind == "slide" and last.slide_number == section.slide_number:
                last.sections.append(section)
            else:
                units.append(_Unit("slide", section.heading_path, [section], section.slide_number))
        elif section.kind == "table":
            units.append(_Unit("table", section.heading_path, [section]))
        elif section.kind == "speaker_notes":
            units.append(_Unit("speaker_notes", section.heading_path, [section]))
        else:
            last = units[-1] if units else None
            if last and last.kind == "body" and last.heading_path == section.heading_path:
                last.sections.append(section)
            else:
                units.append(_Unit("body", section.heading_path, [section]))
    return units


def _unit_text(unit: _Unit) -> str:
    return "\n\n".join(s.text for s in unit.sections)


def _section_path(heading_path: tuple[str, ...], kind: str) -> str | None:
    parts = list(heading_path)
    if kind == "speaker_notes":
        parts.append("Speaker notes")
    return " > ".join(parts) if parts else None


def build_embed_text(
    programme_path: str, document_title: str, section_path: str | None, text: str
) -> str:
    header = " | ".join(part for part in (programme_path, document_title, section_path) if part)
    return f"{header}\n\n{text}" if header else text


def chunk_document(
    sections: list[ParsedSection],
    *,
    document_title: str,
    programme_path: str,
    params: ChunkParams = DEFAULT_PARAMS,
) -> list[DocChunk]:
    """Turn parsed sections into chunks in reading order (ordinals start at 0)."""
    raw: list[
        tuple[str, tuple[str, ...], str, int | None, int | None, int | None, dict[str, Any]]
    ] = []  # kind, heading path, text, page_start, page_end, slide_number, extra metadata

    units = _group(sections)
    pending: list[_Unit] = []  # consecutive small slides waiting to be merged

    def flush_pending() -> None:
        if not pending:
            return
        text = "\n\n".join(_unit_text(u) for u in pending)
        titles: list[str] = []
        for unit in pending:
            title = " > ".join(unit.heading_path)
            if title and title not in titles:
                titles.append(title)
        heading = (" / ".join(titles)[:200],) if titles else ()
        first, last = pending[0].slide_number, pending[-1].slide_number
        extra: dict[str, Any] = {"slide_end": last} if last != first else {}
        raw.append(("slide", heading, text, None, None, first, extra))
        pending.clear()

    for unit in units:
        if unit.kind == "slide":
            text = _unit_text(unit)
            tokens = count_tokens(text)
            if tokens > params.max_tokens:
                flush_pending()
                atoms = _atoms_from_sections(unit.sections, params, by_row=False)
                for piece in _pack(atoms, params):
                    extra = {"hard_split": True} if any(a.hard for a in piece.atoms) else {}
                    raw.append(
                        ("slide", unit.heading_path, _join(piece.atoms), None, None,
                         unit.slide_number, extra)
                    )  # fmt: skip
                continue
            small = tokens < params.merge_slides_below_tokens
            if small and pending:
                same_parent = pending[-1].heading_path[:-1] == unit.heading_path[:-1]
                merged = count_tokens("\n\n".join(_unit_text(u) for u in [*pending, unit]))
                if same_parent and merged <= params.target_tokens:
                    pending.append(unit)
                    continue
            flush_pending()
            if small:
                pending.append(unit)
            else:
                raw.append(("slide", unit.heading_path, text, None, None, unit.slide_number, {}))
            continue
        flush_pending()
        by_row = unit.kind == "table"
        atoms = _atoms_from_sections(unit.sections, params, by_row=by_row)
        if not atoms:
            continue
        for piece in _pack(atoms, params, overlap=not by_row):
            start, end = _page_range(piece.atoms)
            extra = {"hard_split": True} if any(a.hard for a in piece.atoms) else {}
            raw.append(
                (unit.kind, unit.heading_path, _join(piece.atoms), start, end,
                 unit.slide_number, extra)
            )  # fmt: skip
    flush_pending()

    chunks: list[DocChunk] = []
    base_meta = {"chunker": params.as_metadata()}
    for ordinal, (kind, heading, text, p_start, p_end, slide, extra) in enumerate(raw):
        section_path = _section_path(heading, kind)
        chunks.append(
            DocChunk(
                ordinal=ordinal,
                text=text,
                embed_text=build_embed_text(programme_path, document_title, section_path, text),
                token_count=count_tokens(text),
                page_start=p_start,
                page_end=p_end,
                slide_number=slide,
                section_path=section_path,
                metadata={**base_meta, "kind": kind, **extra},
            )
        )
    return chunks
