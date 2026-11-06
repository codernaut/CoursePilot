"""Enqueue side of the job queue. The claim/run side (worker) is added in M3."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.models import Job
from app.models.enums import JobStatus, JobType

DEFAULT_MAX_ATTEMPTS = 3


def enqueue(
    db: AsyncSession,
    job_type: JobType,
    payload: dict[str, Any],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> Job:
    """Add a queued job to the current transaction (the caller commits).

    The payload carries ids only, never document text.
    """
    job = Job(
        type=job_type,
        payload=payload,
        status=JobStatus.QUEUED,
        attempts=0,
        max_attempts=max_attempts,
        run_after=utcnow(),
    )
    db.add(job)
    return job
