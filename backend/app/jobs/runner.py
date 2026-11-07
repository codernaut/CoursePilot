"""Runs one job at a time: claim, execute the handler, record the outcome.

* The handler runs as a task; a heartbeat task refreshes the lock while it works.
* On shutdown (``stop`` set) the running job gets ``grace_s`` seconds to finish; after that it is
  cancelled and released back to the queue (the attempt is not counted).
* Handlers raise ``JobError`` for expected problems (readable message, ``permanent`` or not); any
  other exception is treated as transient and retried with backoff.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import structlog

from app.core.logging import get_logger
from app.jobs.base import ClaimedJob, FailureOutcome, JobError, JobQueue
from app.models.enums import JobType

log = get_logger(__name__)

Handler = Callable[[ClaimedJob], Awaitable[None]]
# (job, readable message, terminal): called after a failed attempt. ``terminal`` is True when the
# job will not be retried; otherwise it was re-queued.
FailureHook = Callable[[ClaimedJob, str, bool], Awaitable[None]]
# Called when a running job is given back during shutdown.
ReleaseHook = Callable[[ClaimedJob], Awaitable[None]]

STALE_MESSAGE = "Processing was interrupted repeatedly (the worker stopped). Try re-indexing."


class JobRunner:
    def __init__(
        self,
        queue: JobQueue,
        handlers: Mapping[JobType, Handler],
        worker_id: str,
        *,
        heartbeat_interval_s: float = 300.0,
        on_failure: FailureHook | None = None,
        on_release: ReleaseHook | None = None,
    ) -> None:
        self._queue = queue
        self._handlers = handlers
        self.worker_id = worker_id
        self._heartbeat_interval = heartbeat_interval_s
        self._on_failure = on_failure
        self._on_release = on_release

    async def run_next(self, stop: asyncio.Event, *, grace_s: float = 20.0) -> bool:
        """Run at most one job. Returns True when a job was claimed (whatever its outcome)."""
        for stale in await self._queue.fail_exhausted():
            log.warning("job_failed_stale", job_id=stale.id, type=stale.type.value)
            await self._notify_failure(stale, STALE_MESSAGE, terminal=True)
        job = await self._queue.claim(self.worker_id)
        if job is None:
            return False
        # Every log line of this job (including the handler task, which copies the context)
        # carries the job id, type and document id.
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            job_id=job.id, job_type=job.type.value, document_id=job.payload.get("document_id")
        )
        try:
            log.info("job_started", attempt=job.attempts)
            handler = self._handlers.get(job.type)
            if handler is None:
                await self._record_failure(job, f"No handler for job type {job.type.value}", True)
                return True
            await self._execute(job, handler, stop, grace_s)
            return True
        finally:
            structlog.contextvars.clear_contextvars()

    async def _execute(
        self, job: ClaimedJob, handler: Handler, stop: asyncio.Event, grace_s: float
    ) -> None:
        task: asyncio.Future[Any] = asyncio.ensure_future(handler(job))
        beat = asyncio.create_task(self._heartbeat(job))
        stopper: asyncio.Future[Any] = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({task, stopper}, return_when=asyncio.FIRST_COMPLETED)
            if not task.done():  # shutdown requested while the job runs
                await asyncio.wait({task}, timeout=grace_s)
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                released = await self._queue.release(job)
                log.info("job_released", job_id=job.id, released=released)
                if self._on_release is not None:
                    with contextlib.suppress(Exception):
                        await self._on_release(job)
                return
        finally:
            beat.cancel()
            stopper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
            with contextlib.suppress(asyncio.CancelledError):
                await stopper
        error = task.exception()
        if error is None:
            if await self._queue.complete(job):
                log.info("job_done", job_id=job.id, type=job.type.value)
            else:
                log.warning("job_lock_lost", job_id=job.id)
            return
        if isinstance(error, JobError):
            await self._record_failure(job, error.message, error.permanent)
            return
        log.warning("job_crashed", job_id=job.id, error=type(error).__name__, exc_info=error)
        await self._record_failure(
            job,
            f"Processing failed unexpectedly ({type(error).__name__}).",
            False,
            detail=f"{type(error).__name__}: {error}",
        )

    async def _record_failure(
        self, job: ClaimedJob, message: str, permanent: bool, *, detail: str | None = None
    ) -> None:
        outcome = await self._queue.fail(job, detail or message, permanent=permanent)
        log.info("job_failed", job_id=job.id, outcome=outcome.value, error=message)
        if outcome is FailureOutcome.LOST:
            return
        await self._notify_failure(job, message, terminal=outcome is FailureOutcome.FAILED)

    async def _notify_failure(self, job: ClaimedJob, message: str, *, terminal: bool) -> None:
        if self._on_failure is None:
            return
        try:
            await self._on_failure(job, message, terminal)
        except Exception as exc:  # the job state is already recorded; do not crash the worker
            log.warning("job_failure_hook_failed", job_id=job.id, error=type(exc).__name__)

    async def _heartbeat(self, job: ClaimedJob) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            try:
                await self._queue.heartbeat(job)
            except Exception as exc:
                log.warning("job_heartbeat_failed", job_id=job.id, error=type(exc).__name__)
