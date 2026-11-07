"""Query embedding with a short in-process cache (CLAUDE.md section 13, cost controls).

Identical queries within ``TTL_S`` reuse the vector instead of calling the embedding provider
again. The key contains provider, model and dimension, so changing the embedding setup never
returns a stale vector of another model. The cache is bounded and per process.
"""

import time
from collections import OrderedDict
from collections.abc import Callable

from app.core.config import Settings
from app.ingestion.embedder import Embedder, get_embedder

TTL_S = 600.0
MAX_ENTRIES = 512

CacheKey = tuple[str, str, int, str]


def normalise_query(query: str) -> str:
    return " ".join(query.split()).casefold()


class QueryEmbeddingCache:
    def __init__(
        self,
        *,
        ttl_s: float = TTL_S,
        max_entries: int = MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_s
        self._max = max_entries
        self._clock = clock
        self._items: OrderedDict[CacheKey, tuple[float, list[float]]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._items)

    def get(self, key: CacheKey) -> list[float] | None:
        entry = self._items.get(key)
        if entry is None:
            return None
        stored_at, vector = entry
        if self._clock() - stored_at >= self._ttl:
            del self._items[key]
            return None
        self._items.move_to_end(key)
        return vector

    def put(self, key: CacheKey, vector: list[float]) -> None:
        now = self._clock()
        self._items[key] = (now, vector)
        self._items.move_to_end(key)
        if len(self._items) > self._max:
            for old_key in [k for k, (t, _) in self._items.items() if now - t >= self._ttl]:
                del self._items[old_key]
        while len(self._items) > self._max:
            self._items.popitem(last=False)


class QueryEmbedder:
    """Lazily creates the configured embedder (cheap; no network until the first query) and
    caches query vectors. ``get_embedder`` raises ``EmbeddingNotConfiguredError`` without a key."""

    def __init__(
        self,
        settings: Settings,
        *,
        factory: Callable[[], Embedder] | None = None,
        cache: QueryEmbeddingCache | None = None,
    ) -> None:
        self._settings = settings
        self._factory = factory or (lambda: get_embedder(settings))
        self._embedder: Embedder | None = None
        self.cache = cache or QueryEmbeddingCache()

    def _get(self) -> Embedder:
        if self._embedder is None:
            self._embedder = self._factory()
        return self._embedder

    def ensure_configured(self) -> None:
        """Raise ``EmbeddingNotConfiguredError`` now instead of at the first query."""
        self._get()

    async def embed(self, query: str) -> list[float]:
        embedder = self._get()  # raises when the provider is not configured
        key: CacheKey = (embedder.provider, embedder.model, embedder.dim, normalise_query(query))
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        vector = await embedder.embed_query(query)
        self.cache.put(key, vector)
        return vector

    async def aclose(self) -> None:
        if self._embedder is not None:
            await self._embedder.aclose()
            self._embedder = None
