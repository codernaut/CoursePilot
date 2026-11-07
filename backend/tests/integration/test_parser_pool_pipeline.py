"""The pipeline parses in a killable child process when a parser pool is configured: a hung parser
fails the document with a readable error and the worker carries on with the next one."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.ingestion import pipeline
from app.ingestion.handlers import build_failure_hook, build_handlers
from app.ingestion.parser_pool import ParserPool
from app.jobs import JobRunner, SqliteJobQueue
from app.models.enums import DocumentStatus, JobType
from tests import parser_support
from tests.ingest_support import Env
from tests.search_support import HEAPS


@pytest.fixture
async def pool() -> AsyncIterator[ParserPool]:
    created = ParserPool(grace_s=0.0)
    yield created
    await created.aclose()


async def test_documents_are_parsed_in_the_child_process(ingest_env: Env, pool: ParserPool) -> None:
    env = ingest_env
    env.deps.parser_pool = pool
    doc = await env.add_document(HEAPS, "heaps.md", title="Heaps")
    assert await pipeline.process_document(env.deps, doc.id) is pipeline.Outcome.READY
    row = await env.document(doc.id)
    assert row is not None and row.status is DocumentStatus.READY
    assert len(await env.chunks(doc.id)) == 1 and pool.restarts == 0


async def test_a_hung_parse_fails_the_document_readably_and_the_next_one_still_works(
    tmp_path: Path, template_db: Path
) -> None:
    from tests.ingest_support import build_env

    env = await build_env(tmp_path, template_db, parse_timeout_s=1)
    pool = ParserPool(parser_support.slow_parse, grace_s=0.0)
    env.deps.parser_pool = pool
    try:
        hung = await env.add_document(HEAPS, "hung.md", title="Hung", job=JobType.INGEST_DOCUMENT)
        queue = SqliteJobQueue(env.database, lock_timeout_s=600)
        runner = JobRunner(
            queue, build_handlers(env.deps), "w", on_failure=build_failure_hook(env.deps)
        )
        assert await runner.run_next(asyncio.Event())
        row = await env.document(hung.id)
        assert row is not None and row.status is DocumentStatus.FAILED
        assert row.error is not None and "took longer than 1 seconds" in row.error
        assert pool.restarts == 1

        # the pool is recreated: the next document parses normally
        pool._parse_fn = parser_support.ok_parse
        fine = await env.add_document(b"hello world", "fine.md", title="Fine")
        assert await pipeline.process_document(env.deps, fine.id) is pipeline.Outcome.READY
    finally:
        await pool.aclose()
        await env.close()
