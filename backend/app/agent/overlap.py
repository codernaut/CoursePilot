"""What the model sees of each retrieved passage (CLAUDE.md section 8).

Consecutive chunks of one document overlap by 80-120 tokens (the chunker's sentence overlap). When
both are retrieved, the model would read the same sentences twice. ``model_texts`` removes the
duplicated leading text of the LATER chunk from what the model sees, then cuts every passage to the
character budget at a word boundary. Nothing stored changes: ``RetrievedChunk.text``, the page and
slide numbers, the ``[C#]`` numbering and the passage viewer keep the full text.

An overlap is only removed when the earlier chunk's tail is actually visible to the model (the
earlier chunk fits in the budget after its own trimming); otherwise the overlapped words would
vanish from the prompt altogether.
"""

import re
from collections.abc import Sequence

from app.retrieval.hybrid import RetrievedChunk, truncate_text

MIN_OVERLAP_CHARS = 40  # shorter matches are accidental (a repeated heading, "see above"...)
_WORD = re.compile(r"\S+")


def overlap_prefix_end(earlier: str, later: str) -> int:
    """The index in ``later`` where its non-duplicated part starts, or 0 when ``later`` does not
    begin with a suffix of ``earlier`` (compared on whitespace-normalised words, at least
    ``MIN_OVERLAP_CHARS`` characters, and never the whole of ``later``)."""
    a = earlier.split()
    spans = [m.span() for m in _WORD.finditer(later)]
    b = [later[s:e] for s, e in spans]
    for k in range(min(len(a), len(b) - 1), 0, -1):  # longest first; keep at least one new word
        if a[-k:] == b[:k]:
            if len(" ".join(b[:k])) < MIN_OVERLAP_CHARS:
                return 0  # shorter suffixes are shorter still
            return spans[k - 1][1]
    return 0


def trim_overlap(earlier: str, later: str) -> str:
    """``later`` without the leading text that repeats the tail of ``earlier``."""
    end = overlap_prefix_end(earlier, later)
    return later[end:].lstrip() if end else later


def model_texts(chunks: Sequence[RetrievedChunk], max_chars: int) -> list[str]:
    """The text of each chunk (same order) that goes into the prompt."""
    by_position = {(c.document_id, c.ordinal): i for i, c in enumerate(chunks)}
    texts = [c.text for c in chunks]
    # Process in document order so a chain A-B-C compares each chunk with the original tail of the
    # previous one, and only trims when that tail is visible after the previous chunk's own trim.
    order = sorted(range(len(chunks)), key=lambda i: (chunks[i].document_id, chunks[i].ordinal))
    for i in order:
        previous = by_position.get((chunks[i].document_id, chunks[i].ordinal - 1))
        if previous is None or previous == i:
            continue
        if len(texts[previous]) > max_chars:
            continue  # the earlier chunk is cut: its tail (the overlap) is not shown
        texts[i] = trim_overlap(chunks[previous].text, texts[i])
    return [truncate_text(t, max_chars) for t in texts]
