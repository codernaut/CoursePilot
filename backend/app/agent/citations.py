"""Citation registry, marker parsing and validation (CLAUDE.md section 9).

Material sources are ``C1``, ``C2``... (the numbered ``<source>`` blocks given to the model); web
sources are ``W1``, ``W2``... (the ``<web_result>`` blocks, M6). After generation every marker
is checked against the registry: unknown markers are removed from the text and only the
sources that were actually cited are returned. Markers inside code spans or fenced code are
left alone and do not count.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.retrieval.hybrid import RetrievedChunk
from app.websearch.base import WebResult

# [C1], [W12], [C1, C2] (grouped) and runs of adjacent brackets such as [C1][C2]; the blanks
# before a run are consumed together with it when none of its markers is valid.
_ID = r"[CW]\d{1,3}"
_GROUP = rf"\[{_ID}(?:\s*[,;]\s*{_ID})*\]"
_RUN = re.compile(rf"(?P<ws>[ \t]*)(?P<run>(?:{_GROUP})+)")
_ID_FINDER = re.compile(_ID)
# fenced blocks first, then inline code spans
_CODE = re.compile(r"```.*?(?:```|\Z)|`[^`\n]*`", re.DOTALL)

MATERIAL_PREFIX = "C"
WEB_PREFIX = "W"
SNIPPET_CHARS = 300


@dataclass(frozen=True)
class Registry:
    """``marker -> source`` for the sources given to the model, in marker order."""

    material: dict[str, RetrievedChunk] = field(default_factory=dict)
    web: dict[str, WebResult] = field(default_factory=dict)

    @classmethod
    def from_chunks(cls, chunks: Sequence[RetrievedChunk]) -> "Registry":
        return cls.from_sources(chunks, ())

    @classmethod
    def from_sources(cls, chunks: Sequence[RetrievedChunk], web: Sequence[WebResult]) -> "Registry":
        return cls(
            {f"{MATERIAL_PREFIX}{i}": chunk for i, chunk in enumerate(chunks, start=1)},
            {f"{WEB_PREFIX}{i}": result for i, result in enumerate(web, start=1)},
        )

    def __contains__(self, marker: object) -> bool:
        return marker in self.material or marker in self.web

    def pairs(self) -> list[tuple[str, RetrievedChunk]]:
        return list(self.material.items())

    def web_pairs(self) -> list[tuple[str, WebResult]]:
        return list(self.web.items())


@dataclass(frozen=True)
class Validated:
    text: str
    cited: list[str]  # valid markers in order of first appearance
    dropped: list[str]  # markers that were not in the registry, in order of appearance


def _validate_segment(
    segment: str, registry: Registry, cited: list[str], dropped: list[str]
) -> str:
    def replace(match: re.Match[str]) -> str:
        ids = _ID_FINDER.findall(match.group("run"))
        valid = [i for i in ids if i in registry]
        dropped.extend(i for i in ids if i not in registry)
        for marker in valid:
            if marker not in cited:
                cited.append(marker)
        if not valid:
            return ""
        return match.group("ws") + "".join(f"[{marker}]" for marker in valid)

    return _RUN.sub(replace, segment)


def validate_citations(text: str, registry: Registry) -> Validated:
    """Remove markers that are not in ``registry`` and report which valid ones are cited."""
    cited: list[str] = []
    dropped: list[str] = []
    out: list[str] = []
    position = 0
    for code in _CODE.finditer(text):
        out.append(_validate_segment(text[position : code.start()], registry, cited, dropped))
        out.append(code.group(0))
        position = code.end()
    out.append(_validate_segment(text[position:], registry, cited, dropped))
    return Validated("".join(out), cited, dropped)


def strip_markers(text: str) -> str:
    """The text without any ``[C#]`` / ``[W#]`` marker (used for the stored conversation memory)."""
    return validate_citations(text, Registry()).text


# an unfinished marker at the very end: "[", "[C", "[C1", "[C1,", "[C1, W"
_PARTIAL_TAIL = re.compile(r"[ \t]*\[(?:\s*[CW]\d{0,3}(?:\s*[,;]\s*(?:[CW]\d{0,3})?)*)?\s*\Z")


def strip_partial_marker(text: str) -> str:
    """Remove a citation marker that an output-length cut left half written at the end of ``text``
    (``... as shown [C1, C``), so a marker is never shown cut in half."""
    return _PARTIAL_TAIL.sub("", text)


def citation_payload(marker: str, chunk: RetrievedChunk) -> dict[str, Any]:
    """The structured source the UI shows for a marker (also stored in ``messages.citations``).
    ``kind`` (``material`` | ``web``) is the key the UI branches on; ``type`` is the same value."""
    flat = " ".join(chunk.text.split())
    snippet = flat if len(flat) <= SNIPPET_CHARS else flat[:SNIPPET_CHARS].rstrip() + "…"
    return {
        "marker": marker,
        "kind": "material",
        "type": "material",
        "chunk_id": chunk.chunk_id,
        "document_id": chunk.document_id,
        "document_title": chunk.document_title,
        "module_id": chunk.module_id,
        "path": chunk.path,
        "location": chunk.location,
        "page_start": chunk.page_start,
        "page_end": chunk.page_end,
        "slide_number": chunk.slide_number,
        "section_path": chunk.section_path,
        "snippet": snippet,
    }


def web_citation_payload(marker: str, result: WebResult) -> dict[str, Any]:
    return {
        "marker": marker,
        "kind": "web",
        "type": "web",
        "url": result.url,
        "title": result.title,
        "site": result.site,
        "snippet": result.snippet[:SNIPPET_CHARS],
        "retrieved_at": result.retrieved_at.isoformat(),
    }


def cited_payloads(registry: Registry, cited: Sequence[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for marker in cited:
        if marker in registry.material:
            out.append(citation_payload(marker, registry.material[marker]))
        elif marker in registry.web:
            out.append(web_citation_payload(marker, registry.web[marker]))
    return out
