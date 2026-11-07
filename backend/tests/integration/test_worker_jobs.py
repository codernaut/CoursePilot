"""The worker loop with real jobs: end-to-end processing, shutdown, housekeeping alongside jobs."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import worker
from app.db.base import utcnow
from app.ingestion.embedder import FakeEmbedder
from app.models import Job, User, UserSession
from app.models.enums import DocumentStatus, JobStatus, JobType
from app.services import documents
from tests.docfiles import make_text_pdf
from tests.ingest_support import DIM, MODEL, Env

MARKDOWN = b"# Heaps\n\nA binary heap keeps the largest item at the root of the tree.\n"


async def wait_for(check: Callable[[], Awaitable[bool]], limit_s: float = 15.0) -> None:
    async with asyncio.timeout(limit_s):
        while not await check():  # noqa: ASYNC110 - polling the database
            await asyncio.sleep(0.05)


def start(env: Env, stop: asyncio.Event, **kw: object) -> asyncio.Task[None]:
    return asyncio.create_task(
        worker.run_worker(
            env.settings,
            stop,
            poll_interval_s=0.05,
            embedder_factory=lambda: FakeEmbedder(model=MODEL, dim=DIM),
            **kw,  # type: ignore[arg-type]
        )
    )


async def test_worker_processes_ingest_reindex_and_delete_jobs_end_to_end(ingest_env: Env) -> None:
    env = ingest_env
    doc = await env.add_document(MARKDOWN, "heaps.md", job=JobType.INGEST_DOCUMENT)
    stop = asyncio.Event()
    task = start(env, stop)
    try:

        async def ready() -> bool:
            row = await env.document(doc.id)
            return row is not None and row.status is DocumentStatus.READY

        await wait_for(ready)
        assert len(await env.chunks(doc.id)) == 1
        assert await env.count("chunk_vectors") == 1 and await env.count("chunks_fts") == 1

        # re-index through the service (what the API does), then delete the same way
        async with env.database.session() as db:
            user = (await db.execute(select(User))).scalar_one()
            await documents.request_reindex(db, user, doc.id)
        await wait_for(ready)
        async with env.database.session() as db:
            user = (await db.execute(select(User))).scalar_one()
            await documents.request_delete(db, user, doc.id)

        async def gone() -> bool:
            return await env.document(doc.id) is None

        await wait_for(gone)
        assert await env.count("chunks") == 0 and await env.count("chunk_vectors") == 0
        assert not await env.storage.exists(doc.storage_key)
        async with env.database.read_session() as db:
            statuses = {j.type: j.status for j in (await db.execute(select(Job))).scalars()}
        assert set(statuses.values()) == {JobStatus.DONE}
        assert set(statuses) == {
            JobType.INGEST_DOCUMENT,
            JobType.REINDEX_DOCUMENT,
            JobType.DELETE_DOCUMENT,
        }
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)


async def test_worker_shows_a_failed_document_with_a_readable_error(ingest_env: Env) -> None:
    env = ingest_env
    doc = await env.add_document(
        make_text_pdf(["", "", ""]), "scan.pdf", job=JobType.INGEST_DOCUMENT
    )
    stop = asyncio.Event()
    task = start(env, stop)
    try:

        async def failed() -> bool:
            row = await env.document(doc.id)
            return row is not None and row.status is DocumentStatus.FAILED

        await wait_for(failed)
        row = await env.document(doc.id)
        assert row is not None and "looks scanned" in (row.error or "")
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)


async def test_graceful_shutdown_releases_a_running_job_and_resets_the_document(
    ingest_env: Env,
) -> None:
    env = ingest_env
    doc = await env.add_document(MARKDOWN, "heaps.md", job=JobType.INGEST_DOCUMENT)
    embedding = asyncio.Event()

    class Stuck(FakeEmbedder):
        async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
            embedding.set()
            await asyncio.sleep(60)
            return []

    stop = asyncio.Event()
    task = asyncio.create_task(
        worker.run_worker(
            env.settings,
            stop,
            poll_interval_s=0.05,
            embedder_factory=lambda: Stuck(model=MODEL, dim=DIM),
            shutdown_grace_s=0.1,
        )
    )
    await asyncio.wait_for(embedding.wait(), 10)
    row = await env.document(doc.id)
    assert row is not None and row.status is DocumentStatus.PROCESSING
    stop.set()
    await asyncio.wait_for(task, 10)  # returns promptly despite the stuck job

    row = await env.document(doc.id)
    assert row is not None and row.status is DocumentStatus.UPLOADED
    async with env.database.read_session() as db:
        job = (await db.execute(select(Job))).scalar_one()
    assert job.status is JobStatus.QUEUED and job.attempts == 0 and job.locked_by is None
    assert await env.count("chunks") == 0

    # a fresh worker picks the released job up and finishes it
    stop2 = asyncio.Event()
    task2 = start(env, stop2)
    try:

        async def ready() -> bool:
            row = await env.document(doc.id)
            return row is not None and row.status is DocumentStatus.READY

        await wait_for(ready)
    finally:
        stop2.set()
        await asyncio.wait_for(task2, 10)


async def test_worker_recovers_a_job_left_running_by_a_crashed_worker(ingest_env: Env) -> None:
    env = ingest_env
    doc = await env.add_document(MARKDOWN, "heaps.md", job=JobType.INGEST_DOCUMENT)
    async with env.database.session() as db:
        job = (await db.execute(select(Job))).scalar_one()
        job.status = JobStatus.RUNNING
        job.attempts = 1
        job.locked_by = "dead-worker"
        job.locked_at = utcnow() - timedelta(seconds=env.settings.job_lock_timeout_s + 60)
        await db.commit()
    stop = asyncio.Event()
    task = start(env, stop)
    try:

        async def ready() -> bool:
            row = await env.document(doc.id)
            return row is not None and row.status is DocumentStatus.READY

        await wait_for(ready)
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)
    async with env.database.read_session() as db:
        job = (await db.execute(select(Job))).scalar_one()
    assert job.status is JobStatus.DONE and job.attempts == 2


async def test_housekeeping_still_runs_while_jobs_are_processed(ingest_env: Env) -> None:
    env = ingest_env
    async with env.database.session() as db:
        user = (await db.execute(select(User))).scalar_one()
        db.add(
            UserSession(
                user_id=user.id,
                token_hash="e" * 64,
                csrf_token="c",
                expires_at=utcnow() - timedelta(seconds=5),
                last_seen_at=utcnow(),
            )
        )
        await db.commit()
    doc = await env.add_document(MARKDOWN, "heaps.md", job=JobType.INGEST_DOCUMENT)
    stop = asyncio.Event()
    task = start(env, stop, interval_s=0.05)
    try:

        async def done() -> bool:
            row = await env.document(doc.id)
            async with env.database.read_session() as db:
                sessions = (await db.execute(select(UserSession))).scalars().all()
            return row is not None and row.status is DocumentStatus.READY and not sessions

        await wait_for(done)
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)


async def test_worker_survives_a_failing_job_loop_and_stops_on_event(
    ingest_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    async def boom(self: object, stop: object, **kw: object) -> bool:
        nonlocal calls
        calls += 1
        raise RuntimeError("database briefly unavailable")

    monkeypatch.setattr("app.jobs.runner.JobRunner.run_next", boom)
    stop = asyncio.Event()
    task = start(ingest_env, stop)
    await asyncio.sleep(0.4)
    assert calls >= 2 and not task.done()
    stop.set()
    await asyncio.wait_for(task, 5)
