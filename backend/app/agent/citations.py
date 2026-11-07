"""Citation registry, marker parsing and validation (CLAUDE.md section 9).

Material sources are ``C1``, ``C2``... (the numbered ``<source>`` blocks given to the model); web
sources (``W1``...) come with M6. After generation every marker is checked against the registry:
unknown markers are removed from the text and only the sources that were actually cited are
returned. Markers inside code spans or fenced code are left alone and do not count.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.retrieval.hybrid import RetrievedChunk

# [C1], [W12], [C1, C2] (grouped) and runs of adjacent brackets such as [C1][C2]; the blanks
# before a run are consumed together with it when none of its markers is valid.
_ID = r"[CW]\d{1,3}"
_GROUP = rf"\[{_ID}(?:\s*[,;]\s*{_ID})*\]"
_RUN = re.compile(rf"(?P<ws>[ \t]*)(?P<run>(?:{_GROUP})+)")
_ID_FINDER = re.compile(_ID)
# fenced blocks first, then inline code spans
_CODE = re.compile(r"```.*?(?:```|\Z)|`[^`\n]*`", re.DOTALL)

MATERIAL_PREFIX = "C"
SNIPPET_CHARS = 300


@dataclass(frozen=True)
class Registry:
    """``marker -> chunk`` for the sources given to the model, in marker order."""

    material: dict[str, RetrievedChunk] = field(default_factory=dict)

    @classmethod
    def from_chunks(cls, chunks: Sequence[RetrievedChunk]) -> "Registry":
        return cls({f"{MATERIAL_PREFIX}{i}": chunk for i, chunk in enumerate(chunks, start=1)})

    def __contains__(self, marker: object) -> bool:
        return marker in self.material

    def pairs(self) -> list[tuple[str, RetrievedChunk]]:
        return list(self.material.items())


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


def citation_payload(marker: str, chunk: RetrievedChunk) -> dict[str, Any]:
    """The structured source the UI shows for a marker (also stored in ``messages.citations``)."""
    flat = " ".join(chunk.text.split())
    snippet = flat if len(flat) <= SNIPPET_CHARS else flat[:SNIPPET_CHARS].rstrip() + "…"
    return {
        "marker": marker,
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


def cited_payloads(registry: Registry, cited: Sequence[str]) -> list[dict[str, Any]]:
    return [citation_payload(m, registry.material[m]) for m in cited if m in registry]
