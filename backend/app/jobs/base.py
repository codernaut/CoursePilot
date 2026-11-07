"""Job queue interface. The worker and the runner depend on this, never on SQLite directly, so the
queue can be swapped for another backend later (CLAUDE.md section 3)."""

import random
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from app.models.enums import JobType

BACKOFF_BASE_S = 30.0
BACKOFF_CAP_S = 3600.0
BACKOFF_JITTER = 0.25
MAX_ERROR_CHARS = 2000


class JobError(Exception):
    """Raised by job handlers.

    ``message`` is readable by the people who see it (faculty see it on the materials page).
    ``permanent`` errors are not retried (a broken file, a missing API key, an index mismatch).
    """

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.permanent = permanent


@dataclass(frozen=True)
class ClaimedJob:
    """A job this worker holds the lock on. ``attempts`` already counts the current attempt."""

    id: str
    type: JobType
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    worker_id: str


class FailureOutcome(StrEnum):
    RETRY = "retry"  # re-queued with a backoff delay
    FAILED = "failed"  # out of attempts or a permanent error
    LOST = "lost"  # this worker no longer holds the lock (someone else owns the job now)


class JobQueue(Protocol):
    async def claim(self, worker_id: str) -> ClaimedJob | None:
        """Atomically take the next runnable job, or None."""

    async def fail_exhausted(self) -> list[ClaimedJob]:
        """Mark stale ``running`` jobs that used all their attempts as failed; return them."""

    async def complete(self, job: ClaimedJob) -> bool:
        """Mark done. False when the lock was lost."""

    async def fail(self, job: ClaimedJob, error: str, *, permanent: bool) -> FailureOutcome:
        """Record a failed attempt: re-queue with exponential backoff, or fail for good."""

    async def heartbeat(self, job: ClaimedJob) -> bool:
        """Refresh the lock so a long job is not mistaken for a crashed one."""

    async def release(self, job: ClaimedJob) -> bool:
        """Give the job back (graceful shutdown): queued again, the attempt not counted."""


def backoff_seconds(
    attempts: int,
    *,
    base: float = BACKOFF_BASE_S,
    cap: float = BACKOFF_CAP_S,
    jitter: float = BACKOFF_JITTER,
    rng: Callable[[], float] = random.random,
) -> float:
    """Delay before attempt ``attempts + 1``: ``base * 2**(attempts-1)`` capped at ``cap``,
    spread by +-``jitter`` so jobs that failed together do not retry together."""
    delay = min(cap, base * 2 ** min(60, max(0, attempts - 1)))  # no float overflow
    return float(delay * (1.0 + jitter * (2.0 * rng() - 1.0)))
