"""A re-index of a ready document keeps its old chunks searchable until the new ones replace them
(CLAUDE.md sections 5 and 7.3); a failed re-index keeps them and shows the error."""

import asyncio
from collections.abc import Sequence

import pytest
from sqlalchemy import select

from app.core.errors import AppError
from app.ingestion import pipeline
from app.ingestion.embedder import EmbeddingError, FakeEmbedder
from app.ingestion.handlers import build_failure_hook, build_handlers
from app.jobs import JobRunner, SqliteJobQueue
from app.models import Chunk, Document, Job, User
from app.models.enums import DocumentStatus, JobStatus
from app.retrieval.hybrid import SearchScope, hybrid_search
from app.services import documents
from tests.ingest_support import DIM, MODEL, Env
from tests.search_support import HEAPS, access_for_faculty

QUERY = "binary heap root of the tree"


async def search_titles(env: Env, query: str = QUERY) -> list[str]:
    vector = await env.embedder.embed_query(query)
    access = await access_for_faculty(env)
    async with env.database.read_session() as db:
        hits = await hybrid_search(
            db,
            access=access,
            scope=SearchScope("all"),
            query=query,
            query_vector=vector,
            filters=None,
            top_k=8,
        )
    return [h.document_title for h in hits]


async def request_reindex(env: Env, document_id: str) -> Document:
    async with env.database.session() as db:
        user = (await db.execute(select(User))).scalar_one()
        return await documents.request_reindex(db, user, document_id)


async def ready_document(env: Env) -> Document:
    doc = await env.add_document(HEAPS, "heaps.md", title="Heaps")
    await pipeline.process_document(env.deps, doc.id)
    return doc


async def test_requesting_a_reindex_keeps_the_document_ready_and_searchable(
    ingest_env: Env,
) -> None:
    env = ingest_env
    doc = await ready_document(env)
    shown = await request_reindex(env, doc.id)
    assert shown.status is DocumentStatus.READY and shown.reindexing is True
    row = await env.document(doc.id)
    assert row is not None and row.status is DocumentStatus.READY and row.reindexing
    assert await search_titles(env) == ["Heaps"]
    # a second request while one is pending is a conflict (and adds no job)
    async with env.database.session() as db:
        user = (await db.execute(select(User))).scalar_one()
        with pytest.raises(AppError) as info:
            await documents.request_reindex(db, user, doc.id)
    assert info.value.status_code == 409
    async with env.database.read_session() as db:
        assert len((await db.execute(select(Job))).scalars().all()) == 1


async def test_old_chunks_stay_searchable_while_the_reindex_runs_then_are_swapped(
    ingest_env: Env,
) -> None:
    env = ingest_env
    doc = await ready_document(env)
    old_seqs = {c.seq for c in await env.chunks(doc.id)}
    await request_reindex(env, doc.id)

    embedding = asyncio.Event()
    release = asyncio.Event()

    class Gated(FakeEmbedder):
        async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
            embedding.set()
            await release.wait()
            return await super().embed_documents(texts)

    env.deps.embedder_factory = lambda: Gated(model=MODEL, dim=DIM)
    queue = SqliteJobQueue(env.database, lock_timeout_s=600)
    runner = JobRunner(
        queue, build_handlers(env.deps), "t", on_failure=build_failure_hook(env.deps)
    )
    running = asyncio.create_task(runner.run_next(asyncio.Event()))
    await asyncio.wait_for(embedding.wait(), 10)

    # mid re-index: still ready, flagged, old chunks untouched and found by search
    row = await env.document(doc.id)
    assert row is not None and row.status is DocumentStatus.READY and row.reindexing
    assert {c.seq for c in await env.chunks(doc.id)} == old_seqs
    assert await search_titles(env) == ["Heaps"]

    release.set()
    await asyncio.wait_for(running, 10)
    row = await env.document(doc.id)
    assert row is not None and row.status is DocumentStatus.READY
    assert row.reindexing is False and row.error is None
    new_seqs = {c.seq for c in await env.chunks(doc.id)}
    assert new_seqs and new_seqs.isdisjoint(old_seqs)
    assert await search_titles(env) == ["Heaps"]


async def test_a_failed_reindex_keeps_the_old_chunks_searchable_and_shows_the_error(
    ingest_env: Env,
) -> None:
    env = ingest_env
    doc = await ready_document(env)
    old_seqs = {c.seq for c in await env.chunks(doc.id)}
    await request_reindex(env, doc.id)

    class Broken(FakeEmbedder):
        async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
            raise EmbeddingError("The embedding key was rejected.", retryable=False)

    env.deps.embedder_factory = lambda: Broken(model=MODEL, dim=DIM)
    queue = SqliteJobQueue(env.database, lock_timeout_s=600)
    runner = JobRunner(
        queue, build_handlers(env.deps), "t", on_failure=build_failure_hook(env.deps)
    )
    assert await runner.run_next(asyncio.Event())

    row = await env.document(doc.id)
    assert row is not None and row.status is DocumentStatus.READY  # not "failed"
    assert row.reindexing is False and row.error == "The embedding key was rejected."
    assert {c.seq for c in await env.chunks(doc.id)} == old_seqs
    assert await search_titles(env) == ["Heaps"]
    async with env.database.read_session() as db:
        job = (await db.execute(select(Job))).scalar_one()
    assert job.status is JobStatus.FAILED

    # a new request is allowed again and clears the error
    again = await request_reindex(env, doc.id)
    assert again.error is None and again.reindexing is True


async def test_a_transient_failure_keeps_showing_reindexing(ingest_env: Env) -> None:
    env = ingest_env
    doc = await ready_document(env)
    await request_reindex(env, doc.id)

    class Flaky(FakeEmbedder):
        async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
            raise EmbeddingError("Service outage.", retryable=True)

    env.deps.embedder_factory = lambda: Flaky(model=MODEL, dim=DIM)
    queue = SqliteJobQueue(env.database, lock_timeout_s=600)
    runner = JobRunner(
        queue, build_handlers(env.deps), "t", on_failure=build_failure_hook(env.deps)
    )
    await runner.run_next(asyncio.Event())
    row = await env.document(doc.id)
    assert row is not None and row.status is DocumentStatus.READY and row.reindexing
    assert await search_titles(env) == ["Heaps"]


async def test_reindexing_a_failed_document_still_shows_queued_and_is_not_searchable(
    ingest_env: Env,
) -> None:
    env = ingest_env
    doc = await env.add_document(HEAPS, "heaps.md", title="Heaps", status=DocumentStatus.FAILED)
    shown = await request_reindex(env, doc.id)
    assert shown.status is DocumentStatus.UPLOADED and shown.reindexing is False
    assert await search_titles(env) == []


async def test_a_document_deleted_during_a_reindex_is_not_resurrected(ingest_env: Env) -> None:
    env = ingest_env
    doc = await ready_document(env)
    await request_reindex(env, doc.id)
    async with env.database.session() as db:
        user = (await db.execute(select(User))).scalar_one()
        await documents.request_delete(db, user, doc.id)
    assert await search_titles(env) == []  # hidden at once
    queue = SqliteJobQueue(env.database, lock_timeout_s=600)
    runner = JobRunner(
        queue, build_handlers(env.deps), "t", on_failure=build_failure_hook(env.deps)
    )
    while await runner.run_next(asyncio.Event()):
        pass
    assert await env.document(doc.id) is None
    async with env.database.read_session() as db:
        assert (await db.execute(select(Chunk))).scalars().all() == []


async def test_reindex_all_clears_a_pending_reindexing_flag_so_a_failure_is_retryable(
    ingest_env: Env,
) -> None:
    """reindex-all makes every document `uploaded`; a stale `reindexing` flag would survive a
    failure and block the faculty re-index button (409) forever."""
    from app.services import reindex

    env = ingest_env
    doc = await ready_document(env)
    await request_reindex(env, doc.id)  # ready + reindexing, job queued
    await reindex.reindex_all(env.database, env.settings)
    row = await env.document(doc.id)
    assert row is not None and row.status is DocumentStatus.UPLOADED and not row.reindexing

    await pipeline.mark_failed(env.database, doc.id, "The embedding key was rejected.")
    row = await env.document(doc.id)
    assert row is not None and row.status is DocumentStatus.FAILED and not row.reindexing
    again = await request_reindex(env, doc.id)  # must not be a 409
    assert again.status is DocumentStatus.UPLOADED
