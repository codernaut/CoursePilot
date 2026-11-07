"""SQLite implementation of the job queue (the ``jobs`` table).

Claiming is ONE ``UPDATE ... WHERE id = (SELECT ... LIMIT 1) RETURNING ...`` statement. Write
sessions start with ``BEGIN IMMEDIATE``, so two workers can never take the same row. A ``running``
job whose lock is older than the lock timeout (a crashed worker) is claimable again, as long as it
has attempts left; ``fail_exhausted`` closes the ones that do not.
"""

import random
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, and_, or_, select, update

from app.db.base import utcnow
from app.db.session import Database
from app.jobs.base import (
    MAX_ERROR_CHARS,
    ClaimedJob,
    FailureOutcome,
    backoff_seconds,
)
from app.models import Job
from app.models.enums import JobStatus

STALE_ERROR = "The worker stopped while processing this job (lock timed out)."


def _truncate(text: str) -> str:
    return text if len(text) <= MAX_ERROR_CHARS else text[: MAX_ERROR_CHARS - 1] + "…"


class SqliteJobQueue:
    def __init__(
        self,
        database: Database,
        *,
        lock_timeout_s: float,
        now: Callable[[], datetime] = utcnow,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._db = database
        self._lock_timeout = timedelta(seconds=lock_timeout_s)
        self._now = now
        self._rng = rng

    def _owned(self, job: ClaimedJob) -> ColumnElement[bool]:
        return and_(
            Job.id == job.id, Job.status == JobStatus.RUNNING, Job.locked_by == job.worker_id
        )

    async def claim(self, worker_id: str) -> ClaimedJob | None:
        now = self._now()
        stale_before = now - self._lock_timeout
        runnable = (
            select(Job.id)
            .where(
                Job.attempts < Job.max_attempts,
                or_(
                    and_(Job.status == JobStatus.QUEUED, Job.run_after <= now),
                    and_(Job.status == JobStatus.RUNNING, Job.locked_at < stale_before),
                ),
            )
            .order_by(Job.run_after, Job.created_at, Job.id)
            .limit(1)
            .scalar_subquery()
        )
        statement = (
            update(Job)
            .where(Job.id == runnable)
            .values(
                status=JobStatus.RUNNING,
                locked_at=now,
                locked_by=worker_id,
                attempts=Job.attempts + 1,
                updated_at=now,
            )
            .returning(Job.id, Job.type, Job.payload, Job.attempts, Job.max_attempts)
            .execution_options(synchronize_session=False)
        )
        async with self._db.session() as session:
            row = (await session.execute(statement)).first()
            await session.commit()
        if row is None:
            return None
        return ClaimedJob(
            id=row.id,
            type=row.type,
            payload=dict(row.payload),
            attempts=row.attempts,
            max_attempts=row.max_attempts,
            worker_id=worker_id,
        )

    async def fail_exhausted(self) -> list[ClaimedJob]:
        now = self._now()
        statement = (
            update(Job)
            .where(
                Job.status == JobStatus.RUNNING,
                Job.locked_at < now - self._lock_timeout,
                Job.attempts >= Job.max_attempts,
            )
            .values(
                status=JobStatus.FAILED,
                last_error=STALE_ERROR,
                locked_at=None,
                locked_by=None,
                updated_at=now,
            )
            .returning(Job.id, Job.type, Job.payload, Job.attempts, Job.max_attempts)
            .execution_options(synchronize_session=False)
        )
        async with self._db.session() as session:
            rows = (await session.execute(statement)).all()
            await session.commit()
        return [
            ClaimedJob(r.id, r.type, dict(r.payload), r.attempts, r.max_attempts, worker_id="")
            for r in rows
        ]

    async def complete(self, job: ClaimedJob) -> bool:
        now = self._now()
        async with self._db.session() as session:
            result = await session.execute(
                update(Job)
                .where(self._owned(job))
                .values(
                    status=JobStatus.DONE,
                    locked_at=None,
                    locked_by=None,
                    last_error=None,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            await session.commit()
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def fail(self, job: ClaimedJob, error: str, *, permanent: bool) -> FailureOutcome:
        now = self._now()
        retry = not permanent and job.attempts < job.max_attempts
        if retry:
            values: dict[str, object] = {
                "status": JobStatus.QUEUED,
                "run_after": now + timedelta(seconds=backoff_seconds(job.attempts, rng=self._rng)),
            }
        else:
            values = {"status": JobStatus.FAILED}
        values.update(locked_at=None, locked_by=None, last_error=_truncate(error), updated_at=now)
        async with self._db.session() as session:
            result = await session.execute(
                update(Job)
                .where(self._owned(job))
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            await session.commit()
        if not result.rowcount:  # type: ignore[attr-defined]
            return FailureOutcome.LOST
        return FailureOutcome.RETRY if retry else FailureOutcome.FAILED

    async def heartbeat(self, job: ClaimedJob) -> bool:
        async with self._db.session() as session:
            result = await session.execute(
                update(Job)
                .where(self._owned(job))
                .values(locked_at=self._now())
                .execution_options(synchronize_session=False)
            )
            await session.commit()
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def release(self, job: ClaimedJob) -> bool:
        now = self._now()
        async with self._db.session() as session:
            result = await session.execute(
                update(Job)
                .where(self._owned(job))
                .values(
                    status=JobStatus.QUEUED,
                    attempts=Job.attempts - 1,
                    run_after=now,
                    locked_at=None,
                    locked_by=None,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            await session.commit()
        return bool(result.rowcount)  # type: ignore[attr-defined]
