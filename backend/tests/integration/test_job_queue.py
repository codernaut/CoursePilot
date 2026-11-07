"""The SQLite job queue and the runner: atomic claim, scheduling, backoff, stale locks, shutdown."""

import asyncio
import threading
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.db.session import Database
from app.jobs import (
    ClaimedJob,
    FailureOutcome,
    JobError,
    JobRunner,
    SqliteJobQueue,
    backoff_seconds,
    enqueue,
)
from app.models import Job
from app.models.enums import JobStatus, JobType

T0 = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


@pytest.fixture
async def database(settings: Settings) -> AsyncIterator[Database]:
    db = Database(settings.database_url)
    yield db
    await db.dispose()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def queue(database: Database, clock: Clock) -> SqliteJobQueue:
    return SqliteJobQueue(database, lock_timeout_s=600, now=clock, rng=lambda: 0.5)


async def add_job(
    database: Database,
    clock: Clock,
    *,
    run_after: datetime | None = None,
    max_attempts: int = 3,
    job_type: JobType = JobType.INGEST_DOCUMENT,
    payload: dict[str, object] | None = None,
) -> str:
    async with database.session() as db:
        job = enqueue(db, job_type, payload or {"document_id": "d"}, max_attempts=max_attempts)
        job.run_after = run_after or clock.now
        job.created_at = clock.now
        await db.commit()
        return job.id


async def load(database: Database, job_id: str) -> Job:
    async with database.read_session() as db:
        job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one()
        return job


# --- claim -----------------------------------------------------------------------------------


async def test_claim_locks_the_job_and_counts_the_attempt(
    database: Database, queue: SqliteJobQueue, clock: Clock
) -> None:
    job_id = await add_job(database, clock)
    claimed = await queue.claim("worker-a")
    assert claimed is not None
    assert (claimed.id, claimed.attempts, claimed.max_attempts) == (job_id, 1, 3)
    assert claimed.type is JobType.INGEST_DOCUMENT and claimed.payload == {"document_id": "d"}
    row = await load(database, job_id)
    assert row.status is JobStatus.RUNNING and row.locked_by == "worker-a"
    assert row.locked_at == clock.now
    assert await queue.claim("worker-b") is None  # nothing else is runnable


async def test_claim_respects_run_after_and_orders_by_it(
    database: Database, queue: SqliteJobQueue, clock: Clock
) -> None:
    later = await add_job(database, clock, run_after=clock.now + timedelta(minutes=5))
    sooner = await add_job(database, clock, run_after=clock.now - timedelta(minutes=1))
    oldest = await add_job(database, clock, run_after=clock.now - timedelta(minutes=9))
    first = await queue.claim("w")
    second = await queue.claim("w")
    assert first is not None and second is not None
    assert (first.id, second.id) == (oldest, sooner)
    assert await queue.claim("w") is None  # `later` is not due yet
    clock.advance(minutes=6)
    third = await queue.claim("w")
    assert third is not None and third.id == later


def _claim_all(url: str, worker: str, clock: Clock, out: list[str]) -> None:
    async def run() -> None:
        db = Database(url)
        try:
            queue = SqliteJobQueue(db, lock_timeout_s=600, now=clock)
            while (job := await queue.claim(worker)) is not None:
                out.append(job.id)
                await asyncio.sleep(0)
        finally:
            await db.dispose()

    asyncio.run(run())


async def test_two_workers_never_claim_the_same_job(
    settings: Settings, database: Database, clock: Clock
) -> None:
    ids = {await add_job(database, clock) for _ in range(40)}
    results: list[list[str]] = [[], []]
    threads = [
        threading.Thread(
            target=_claim_all, args=(settings.database_url, f"w{i}", clock, results[i])
        )
        for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        await asyncio.to_thread(t.join)
    claimed = results[0] + results[1]
    assert len(claimed) == len(set(claimed)) == 40  # no duplicates, nothing missed
    assert set(claimed) == ids
    assert results[0] and results[1]  # both workers took part


# --- outcomes --------------------------------------------------------------------------------


async def test_complete_marks_done_and_only_the_lock_holder_may_do_it(
    database: Database, queue: SqliteJobQueue, clock: Clock
) -> None:
    await add_job(database, clock)
    job = await queue.claim("w1")
    assert job is not None
    stranger = ClaimedJob(job.id, job.type, job.payload, 1, 3, worker_id="w2")
    assert await queue.complete(stranger) is False
    assert await queue.fail(stranger, "x", permanent=True) is FailureOutcome.LOST
    assert (await load(database, job.id)).status is JobStatus.RUNNING
    assert await queue.complete(job) is True
    row = await load(database, job.id)
    assert row.status is JobStatus.DONE and row.locked_by is None and row.last_error is None


def test_backoff_never_overflows_for_absurd_attempt_counts() -> None:
    assert backoff_seconds(5000, rng=lambda: 0.5) == 3600.0


def test_backoff_schedule_is_exponential_capped_and_jittered() -> None:
    mid = lambda: 0.5  # noqa: E731
    assert [backoff_seconds(n, rng=mid) for n in (1, 2, 3, 4)] == [30, 60, 120, 240]
    assert backoff_seconds(30, rng=mid) == 3600  # capped
    assert backoff_seconds(1, rng=lambda: 0.0) == pytest.approx(22.5)  # -25%
    assert backoff_seconds(1, rng=lambda: 1.0) == pytest.approx(37.5)  # +25%


async def test_failures_retry_with_growing_backoff_until_max_attempts(
    database: Database, queue: SqliteJobQueue, clock: Clock
) -> None:
    job_id = await add_job(database, clock, max_attempts=3)
    delays = []
    for attempt in (1, 2):
        job = await queue.claim("w")
        assert job is not None and job.attempts == attempt
        assert await queue.fail(job, f"boom {attempt}", permanent=False) is FailureOutcome.RETRY
        row = await load(database, job_id)
        assert row.status is JobStatus.QUEUED and row.locked_by is None
        assert row.last_error == f"boom {attempt}"
        delays.append((row.run_after - clock.now).total_seconds())
        assert await queue.claim("w") is None  # not due yet
        clock.advance(seconds=delays[-1] + 1)
    assert delays == [30.0, 60.0]
    last = await queue.claim("w")
    assert last is not None and last.attempts == 3
    assert await queue.fail(last, "boom 3", permanent=False) is FailureOutcome.FAILED
    row = await load(database, job_id)
    assert row.status is JobStatus.FAILED and row.attempts == 3 and row.last_error == "boom 3"
    clock.advance(days=1)
    assert await queue.claim("w") is None  # failed jobs are never claimed again


async def test_a_permanent_failure_does_not_retry(
    database: Database, queue: SqliteJobQueue, clock: Clock
) -> None:
    job_id = await add_job(database, clock)
    job = await queue.claim("w")
    assert job is not None
    assert await queue.fail(job, "broken file", permanent=True) is FailureOutcome.FAILED
    assert (await load(database, job_id)).status is JobStatus.FAILED


# --- stale locks, heartbeat, release -----------------------------------------------------------


async def test_stale_running_job_is_requeued_by_the_claim_after_the_lock_timeout(
    database: Database, queue: SqliteJobQueue, clock: Clock
) -> None:
    job_id = await add_job(database, clock)
    crashed = await queue.claim("crashed-worker")
    assert crashed is not None
    clock.advance(seconds=599)
    assert await queue.claim("w2") is None  # lock still fresh
    clock.advance(seconds=2)
    taken = await queue.claim("w2")
    assert taken is not None and taken.id == job_id and taken.attempts == 2
    row = await load(database, job_id)
    assert row.locked_by == "w2"
    assert await queue.complete(crashed) is False  # the old owner lost the lock


async def test_stale_job_out_of_attempts_is_failed_not_rerun(
    database: Database, queue: SqliteJobQueue, clock: Clock
) -> None:
    job_id = await add_job(database, clock, max_attempts=1)
    assert await queue.claim("crashed") is not None
    clock.advance(seconds=700)
    assert await queue.claim("w2") is None
    failed = await queue.fail_exhausted()
    assert [j.id for j in failed] == [job_id]
    row = await load(database, job_id)
    assert row.status is JobStatus.FAILED and "lock timed out" in (row.last_error or "")
    assert await queue.fail_exhausted() == []


async def test_heartbeat_keeps_a_long_job_from_being_taken_over(
    database: Database, queue: SqliteJobQueue, clock: Clock
) -> None:
    await add_job(database, clock)
    job = await queue.claim("w1")
    assert job is not None
    for _ in range(3):
        clock.advance(seconds=500)
        assert await queue.heartbeat(job) is True
        assert await queue.claim("w2") is None
    assert (await load(database, job.id)).locked_at == clock.now


async def test_short_heartbeat_window_recovers_a_dead_worker_but_never_steals_from_a_live_one(
    database: Database, clock: Clock, settings: Settings
) -> None:
    from app.worker import heartbeat_interval_s, stale_after_s

    window = stale_after_s(settings)
    interval = heartbeat_interval_s(settings)
    assert window == min(settings.heartbeat_stale_s, settings.job_lock_timeout_s) <= 120
    assert interval * 4 <= window  # several beats fit in the window
    queue = SqliteJobQueue(database, lock_timeout_s=window, now=clock, rng=lambda: 0.5)
    await add_job(database, clock)
    live = await queue.claim("live")
    assert live is not None
    for _ in range(10):  # a live worker beats every interval: never taken over
        clock.advance(seconds=interval)
        assert await queue.heartbeat(live) is True
        assert await queue.claim("thief") is None
    clock.advance(seconds=window - 1)  # then it dies; still inside the window
    assert await queue.claim("thief") is None
    clock.advance(seconds=2)
    taken = await queue.claim("thief")
    assert taken is not None and taken.id == live.id and taken.attempts == 2
    assert await queue.heartbeat(live) is False and await queue.complete(live) is False


async def test_release_requeues_without_counting_the_attempt(
    database: Database, queue: SqliteJobQueue, clock: Clock
) -> None:
    await add_job(database, clock)
    job = await queue.claim("w1")
    assert job is not None
    assert await queue.release(job) is True
    row = await load(database, job.id)
    assert row.status is JobStatus.QUEUED and row.attempts == 0 and row.locked_by is None
    again = await queue.claim("w1")
    assert again is not None and again.attempts == 1


# --- runner ------------------------------------------------------------------------------------


class Hooks:
    def __init__(self) -> None:
        self.failures: list[tuple[str, str, bool]] = []
        self.released: list[str] = []

    async def on_failure(self, job: ClaimedJob, message: str, terminal: bool) -> None:
        self.failures.append((job.id, message, terminal))

    async def on_release(self, job: ClaimedJob) -> None:
        self.released.append(job.id)


def make_runner(queue: SqliteJobQueue, handler: object, hooks: Hooks, **kw: float) -> JobRunner:
    return JobRunner(
        queue,
        {JobType.INGEST_DOCUMENT: handler},  # type: ignore[dict-item]
        "runner-1",
        on_failure=hooks.on_failure,
        on_release=hooks.on_release,
        **kw,
    )


async def test_runner_runs_a_job_to_done(
    database: Database, queue: SqliteJobQueue, clock: Clock
) -> None:
    seen: list[str] = []

    async def handler(job: ClaimedJob) -> None:
        seen.append(str(job.payload["document_id"]))

    job_id = await add_job(database, clock, payload={"document_id": "doc-1"})
    runner = make_runner(queue, handler, Hooks())
    assert await runner.run_next(asyncio.Event()) is True
    assert seen == ["doc-1"]
    assert (await load(database, job_id)).status is JobStatus.DONE
    assert await runner.run_next(asyncio.Event()) is False  # queue is empty


async def test_runner_records_permanent_and_transient_failures(
    database: Database, queue: SqliteJobQueue, clock: Clock
) -> None:
    async def handler(job: ClaimedJob) -> None:
        if job.payload["document_id"] == "bad":
            raise JobError("This file is broken.", permanent=True)
        raise RuntimeError("database is locked")

    hooks = Hooks()
    earlier = clock.now - timedelta(seconds=2)  # a fixed claim order
    bad = await add_job(database, clock, run_after=earlier, payload={"document_id": "bad"})
    flaky = await add_job(database, clock, payload={"document_id": "flaky"})
    runner = make_runner(queue, handler, hooks)
    await runner.run_next(asyncio.Event())
    await runner.run_next(asyncio.Event())
    assert (await load(database, bad)).status is JobStatus.FAILED
    flaky_row = await load(database, flaky)
    assert flaky_row.status is JobStatus.QUEUED and flaky_row.run_after > clock.now
    assert flaky_row.last_error == "RuntimeError: database is locked"
    assert hooks.failures == [
        (bad, "This file is broken.", True),
        (flaky, "Processing failed unexpectedly (RuntimeError).", False),
    ]


async def test_runner_fails_stale_exhausted_jobs_and_tells_the_hook(
    database: Database, queue: SqliteJobQueue, clock: Clock
) -> None:
    job_id = await add_job(database, clock, max_attempts=1)
    assert await queue.claim("crashed") is not None
    clock.advance(seconds=700)
    hooks = Hooks()
    runner = make_runner(queue, lambda job: asyncio.sleep(0), hooks)
    assert await runner.run_next(asyncio.Event()) is False
    assert [(f[0], f[2]) for f in hooks.failures] == [(job_id, True)]


async def test_shutdown_releases_a_job_that_does_not_finish_in_time(
    database: Database, queue: SqliteJobQueue, clock: Clock
) -> None:
    started = asyncio.Event()
    cancelled = False

    async def handler(job: ClaimedJob) -> None:
        nonlocal cancelled
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled = True
            raise

    job_id = await add_job(database, clock)
    hooks = Hooks()
    runner = make_runner(queue, handler, hooks)
    stop = asyncio.Event()
    task = asyncio.create_task(runner.run_next(stop, grace_s=0.05))
    await asyncio.wait_for(started.wait(), 2)
    stop.set()
    assert await asyncio.wait_for(task, 5) is True
    assert cancelled
    row = await load(database, job_id)
    assert row.status is JobStatus.QUEUED and row.attempts == 0
    assert hooks.released == [job_id] and hooks.failures == []


async def test_shutdown_lets_a_quick_job_finish(
    database: Database, queue: SqliteJobQueue, clock: Clock
) -> None:
    started = asyncio.Event()

    async def handler(job: ClaimedJob) -> None:
        started.set()
        await asyncio.sleep(0.1)

    job_id = await add_job(database, clock)
    runner = make_runner(queue, handler, Hooks())
    stop = asyncio.Event()
    task = asyncio.create_task(runner.run_next(stop, grace_s=5))
    await started.wait()
    stop.set()
    await asyncio.wait_for(task, 5)
    assert (await load(database, job_id)).status is JobStatus.DONE


async def test_runner_heartbeats_while_a_job_runs(
    database: Database, queue: SqliteJobQueue, clock: Clock
) -> None:
    beats = 0
    original = queue.heartbeat

    async def counting(job: ClaimedJob) -> bool:
        nonlocal beats
        beats += 1
        return await original(job)

    queue.heartbeat = counting  # type: ignore[method-assign]

    async def handler(job: ClaimedJob) -> None:
        await asyncio.sleep(0.25)

    await add_job(database, clock)
    runner = make_runner(queue, handler, Hooks(), heartbeat_interval_s=0.05)
    await runner.run_next(asyncio.Event())
    assert beats >= 2
