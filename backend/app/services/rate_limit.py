"""Per-key sliding-window rate limiting for chat and uploads (CLAUDE.md section 13).

The counters live in this process's memory, like the login throttle: they are not shared between
processes and reset on a restart. That is fine for the single uvicorn process CoursePilot runs;
running several web processes would multiply the effective limits. Nothing here imports FastAPI.
"""

import math
import time
from collections import deque
from collections.abc import Callable

from app.core.errors import AppError

_SWEEP_THRESHOLD = 5000


class SlidingWindowLimiter:
    """At most ``limit`` hits per ``window_s`` seconds for each key (here: a user id)."""

    def __init__(
        self,
        limit: int,
        *,
        window_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window = window_s
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def _prune(self, key: str, now: float) -> deque[float]:
        hits = self._hits.get(key)
        if hits is None:
            return deque()
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        if not hits:
            del self._hits[key]
            return deque()
        return hits

    def hit(self, key: str) -> float | None:
        """Record one request. Returns ``None`` when allowed, otherwise the seconds until the
        oldest counted request leaves the window (nothing is recorded when limited)."""
        now = self._clock()
        if len(self._hits) > _SWEEP_THRESHOLD:
            for stale in list(self._hits):
                self._prune(stale, now)
        hits = self._prune(key, now)
        if len(hits) >= self.limit:
            return max(0.0, self.window - (now - hits[0]))
        self._hits.setdefault(key, deque()).append(now)
        return None

    def enforce(self, key: str, what: str) -> None:
        """``hit`` that raises ``429 rate_limited`` with a ``Retry-After`` header."""
        wait = self.hit(key)
        if wait is None:
            return
        seconds = max(1, math.ceil(wait))
        raise AppError(
            "rate_limited",
            f"Too many {what} requests. Please wait {seconds} seconds and try again.",
            status_code=429,
            details={"retry_after_s": seconds, "limit_per_min": self.limit},
            headers={"Retry-After": str(seconds)},
        )
