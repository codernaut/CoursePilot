"""Token counting without a tokenizer download.

Real tokenizers (tiktoken, provider tokenizers) fetch vocabulary files over the network or add a
heavy dependency, and each embedding provider counts differently anyway. Chunk sizes only need to
be roughly right, so we estimate: a word or punctuation run costs one token per ~4 characters
(minimum one), and every CJK character counts as one token. The estimate is deterministic, has no
I/O, and is within about +-25% of common BPE tokenizers on English prose.
"""

import re

COUNTER_NAME = "heuristic-v1"

_PIECES = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]|\w+|[^\w\s]")
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")


def count_tokens(text: str) -> int:
    total = 0
    for piece in _PIECES.findall(text):
        if _CJK.fullmatch(piece):
            total += 1
        else:
            total += max(1, -(-len(piece) // 4))
    return total
