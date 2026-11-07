"""The processing pipeline end to end with the fake embedder and local storage (no network)."""

import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest
import sqlite_vec
from sqlalchemy import select, text

from app.db import chunk_store
from app.ingestion import pipeline
from app.ingestion.embedder import EmbeddingError, FakeEmbedder
from app.ingestion.handlers import build_failure_hook, build_handlers
from app.ingestion.pipeline import Outcome
from app.jobs import JobError, JobRunner, SqliteJobQueue
from app.models import AuditLog, Chunk, Document, Job
from app.models.enums import DocumentStatus, JobStatus, JobType
from app.services import reindex
from tests.docfiles import make_docx, make_pptx, make_text_pdf
from tests.ingest_support import DIM, Env, build_env, chunk_count

MARKDOWN = (
    b"# Sorting\n\nMerge sort splits a list in halves and merges the sorted halves.\n\n"
    b"## Quick sort\n\nQuick sort partitions around a pivot value.\n"
)


async def vector_rows(env: Env) -> list[tuple[int, int]]:
    """(rowid, byte length) of every stored vector."""
    async with env.database.read_session() as db:
        rows = await db.execute(text("SELECT rowid, length(embedding) FROM chunk_vectors"))
        return [(int(r[0]), int(r[1])) for r in rows]


async def fts_rowids(env: Env, query: str) -> list[int]:
    async with env.database.read_session() as db:
        rows = await db.execute(
            text("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH :q ORDER BY rowid"),
            {"q": query},
        )
        return [int(r[0]) for r in rows]


async def knn(env: Env, vector: Sequence[float], k: int = 3) -> list[int]:
    async with env.database.read_session() as db:
        rows = await db.execute(
            text(
                "SELECT rowid FROM chunk_vectors WHERE embedding MATCH :v AND k = :k "
                "ORDER BY distance"
            ),
            {"v": sqlite_vec.serialize_float32(list(vector)), "k": k},
        )
        return [int(r[0]) for r in rows]


# --- the happy path -----------------------------------------------------------------------------


async def test_ingest_creates_chunks_vectors_and_fts_rows_that_line_up(ingest_env: Env) -> None:
    env = ingest_env
    doc = await env.add_document(MARKDOWN, "sorting.md", title="Sorting notes")
    assert await pipeline.process_document(env.deps, doc.id) is Outcome.READY

    stored = await env.document(doc.id)
    assert stored is not None
    assert stored.status is DocumentStatus.READY and stored.error is None
    assert stored.page_count is None  # markdown has no pages
    chunks = await env.chunks(doc.id)
    assert [c.ordinal for c in chunks] == [0, 1]
    first = chunks[0]
    assert first.module_id == env.module_id and first.embedding_model == "fake-hash-v1"
    assert first.section_path == "Sorting"
    assert first.text.startswith("Merge sort splits")
    assert first.embed_text.startswith("Year 1 > Block 1 > Module 1 | Sorting notes | Sorting\n\n")
    assert first.meta["kind"] == "body" and first.meta["chunker"]["max_tokens"] == 800

    # vectors: one per chunk under the same rowid, right size (16 floats x 4 bytes)
    assert sorted(await vector_rows(env)) == [(c.seq, DIM * 4) for c in chunks]
    # full-text: the keyword finds exactly the right chunk, and the header words are indexed too
    assert await fts_rowids(env, '"pivot"') == [chunks[1].seq]
    assert await fts_rowids(env, '"merge"') == [first.seq]
    assert await fts_rowids(env, '"Sorting" AND "notes"') == [first.seq, chunks[1].seq]
    # KNN: a query embedded like the chunk finds that chunk first
    query = await env.embedder.embed_query(chunks[1].embed_text)
    assert (await knn(env, query))[0] == chunks[1].seq
    assert env.embedder.document_calls == [2]  # one batch for the whole document


@pytest.mark.parametrize(
    ("filename", "data", "expect_pages", "needle"),
    [
        (
            "lecture.pdf",
            make_text_pdf(["Hashing maps keys to buckets.", "Collisions need chaining."]),
            2,
            "chaining",
        ),
        (
            "slides.pptx",
            make_pptx([{"title": "Hashing", "bullets": ["Buckets", "Chaining"]}]),
            1,
            "chaining",
        ),
        (
            "handout.docx",
            make_docx([("h1", "Hashing"), ("p", "Chaining resolves collisions.")]),
            None,
            "chaining",
        ),
        ("notes.txt", b"Open addressing probes for a free slot.", None, "probes"),
        ("page.html", b"<h1>Hashing</h1><p>Rehashing doubles the table.</p>", None, "rehashing"),
    ],
)
async def test_every_file_type_is_processed(
    ingest_env: Env, filename: str, data: bytes, expect_pages: int | None, needle: str
) -> None:
    doc = await ingest_env.add_document(data, filename)
    assert await pipeline.process_document(ingest_env.deps, doc.id) is Outcome.READY
    stored = await ingest_env.document(doc.id)
    assert stored is not None and stored.status is DocumentStatus.READY
    assert stored.page_count == expect_pages
    assert len(await fts_rowids(ingest_env, f'"{needle}"')) >= 1


async def test_pdf_chunks_carry_page_numbers_and_pptx_chunks_slide_numbers(ingest_env: Env) -> None:
    pdf = await ingest_env.add_document(
        make_text_pdf(
            ["First page text of the lecture.", "Second page text of the lecture."],
            toc=[[1, "Part", 1]],
        ),
        "a.pdf",
    )
    pptx = await ingest_env.add_document(
        make_pptx(
            [
                {"title": "One", "bullets": [" ".join(["alpha"] * 200)]},
                {"title": "Two", "bullets": [" ".join(["beta"] * 200)]},
            ]
        ),
        "b.pptx",
    )
    await pipeline.process_document(ingest_env.deps, pdf.id)
    await pipeline.process_document(ingest_env.deps, pptx.id)
    (pdf_chunk,) = await ingest_env.chunks(pdf.id)
    assert (pdf_chunk.page_start, pdf_chunk.page_end, pdf_chunk.slide_number) == (1, 2, None)
    assert pdf_chunk.section_path == "Part"
    slides = await ingest_env.chunks(pptx.id)
    assert [c.slide_number for c in slides] == [1, 2]
    assert all(c.page_start is None for c in slides)


# --- idempotency and transactions --------------------------------------------------------------


async def test_processing_twice_gives_the_same_rows(ingest_env: Env) -> None:
    env = ingest_env
    doc = await env.add_document(MARKDOWN, "sorting.md")
    await pipeline.process_document(env.deps, doc.id)
    first = [(c.ordinal, c.text, c.embed_text, c.token_count) for c in await env.chunks(doc.id)]
    await pipeline.process_document(env.deps, doc.id)
    second = [(c.ordinal, c.text, c.embed_text, c.token_count) for c in await env.chunks(doc.id)]
    assert first == second
    assert await env.count("chunks") == await env.count("chunk_vectors") == 2
    assert await env.count("chunks_fts") == 2  # old rows were deleted, not accumulated


async def test_failure_before_the_write_leaves_old_chunks_untouched(ingest_env: Env) -> None:
    env = ingest_env
    doc = await env.add_document(MARKDOWN, "sorting.md")
    await pipeline.process_document(env.deps, doc.id)
    before = [(c.seq, c.text) for c in await env.chunks(doc.id)]

    class Broken(FakeEmbedder):
        async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
            raise EmbeddingError("The Fake embedding service timed out.", retryable=True)

    env.deps.embedder_factory = lambda: Broken(model="fake-hash-v1", dim=DIM)
    with pytest.raises(JobError) as info:
        await pipeline.process_document(env.deps, doc.id)
    assert info.value.permanent is False and "timed out" in info.value.message
    assert [(c.seq, c.text) for c in await env.chunks(doc.id)] == before
    assert len(await vector_rows(env)) == 2 and await env.count("chunks_fts") == 2


async def test_failure_inside_the_write_transaction_rolls_everything_back(
    ingest_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = ingest_env
    doc = await env.add_document(MARKDOWN, "sorting.md")
    await pipeline.process_document(env.deps, doc.id)
    before = [(c.seq, c.text) for c in await env.chunks(doc.id)]
    original = chunk_store.insert_chunks

    async def explode(db, chunks, vectors):  # type: ignore[no-untyped-def]
        await original(db, chunks, vectors)  # rows are in the transaction ...
        raise RuntimeError("disk full")  # ... when it fails

    monkeypatch.setattr(chunk_store, "insert_chunks", explode)
    with pytest.raises(RuntimeError, match="disk full"):
        await pipeline.process_document(env.deps, doc.id)
    # the delete of the old rows was rolled back too
    assert [(c.seq, c.text) for c in await env.chunks(doc.id)] == before
    assert sorted(r[0] for r in await vector_rows(env)) == [s for s, _ in before]
    assert await env.count("chunks_fts") == 2
    stored = await env.document(doc.id)
    assert stored is not None and stored.status is DocumentStatus.PROCESSING  # set before the work


# --- delete races and the delete job ------------------------------------------------------------


async def test_a_document_deleted_while_processing_never_becomes_ready(ingest_env: Env) -> None:
    env = ingest_env
    doc = await env.add_document(MARKDOWN, "sorting.md")

    class DeletesWhileEmbedding(FakeEmbedder):
        async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
            async with env.database.session() as db:
                row = await db.get(Document, doc.id)
                assert row is not None
                row.status = DocumentStatus.DELETING
                await db.commit()
            return await super().embed_documents(texts)

    env.deps.embedder_factory = lambda: DeletesWhileEmbedding(model="fake-hash-v1", dim=DIM)
    assert await pipeline.process_document(env.deps, doc.id) is Outcome.DISCARDED
    stored = await env.document(doc.id)
    assert stored is not None and stored.status is DocumentStatus.DELETING
    assert await chunk_count(env.database) == 0
    assert await env.count("chunk_vectors") == 0 and await env.count("chunks_fts") == 0


async def test_a_deleting_document_is_skipped_and_a_missing_one_too(ingest_env: Env) -> None:
    env = ingest_env
    doc = await env.add_document(MARKDOWN, status=DocumentStatus.DELETING)
    assert await pipeline.process_document(env.deps, doc.id) is Outcome.DISCARDED
    stored = await env.document(doc.id)
    assert stored is not None and stored.status is DocumentStatus.DELETING
    assert await pipeline.process_document(env.deps, "0" * 32) is Outcome.DISCARDED


async def test_delete_job_removes_rows_vectors_fts_object_document_and_audits(
    ingest_env: Env,
) -> None:
    env = ingest_env
    doc = await env.add_document(MARKDOWN, "sorting.md")
    other = await env.add_document(b"Another document about graphs.", "graphs.txt")
    await pipeline.process_document(env.deps, doc.id)
    await pipeline.process_document(env.deps, other.id)
    async with env.database.session() as db:
        row = await db.get(Document, doc.id)
        assert row is not None
        row.status = DocumentStatus.DELETING
        await db.commit()

    assert await pipeline.delete_document(env.deps, doc.id) is True
    assert await env.document(doc.id) is None
    assert await env.chunks(doc.id) == []
    assert not await env.storage.exists(doc.storage_key)
    assert await env.storage.exists(other.storage_key)  # unrelated objects stay
    remaining = await env.chunks(other.id)
    assert len(remaining) == 1
    assert [r[0] for r in await vector_rows(env)] == [remaining[0].seq]
    assert await env.count("chunks_fts") == 1
    async with env.database.read_session() as db:
        audits = (
            (await db.execute(select(AuditLog).where(AuditLog.action == "document_deleted")))
            .scalars()
            .all()
        )
    assert len(audits) == 1 and audits[0].target_id == doc.id
    assert audits[0].details["chunks_removed"] == 2
    assert await pipeline.delete_document(env.deps, doc.id) is False  # idempotent


async def test_delete_job_ignores_a_document_that_is_not_deleting(ingest_env: Env) -> None:
    doc = await ingest_env.add_document(MARKDOWN)
    assert await pipeline.delete_document(ingest_env.deps, doc.id) is False
    assert await ingest_env.document(doc.id) is not None
    assert await ingest_env.storage.exists(doc.storage_key)


# --- failures are readable ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "filename", "message"),
    [
        (make_text_pdf(["", "", ""]), "scan.pdf", "looks scanned"),
        (b"%PDF-1.4 garbage", "broken.pdf", "damaged"),
        (b"   \n ", "empty.txt", "No readable text"),
    ],
)
async def test_unreadable_files_raise_permanent_readable_errors(
    ingest_env: Env, data: bytes, filename: str, message: str
) -> None:
    doc = await ingest_env.add_document(data, filename)
    with pytest.raises(JobError, match=message) as info:
        await pipeline.process_document(ingest_env.deps, doc.id)
    assert info.value.permanent is True


async def test_missing_stored_file_is_a_readable_permanent_error(ingest_env: Env) -> None:
    doc = await ingest_env.add_document(MARKDOWN)
    await ingest_env.storage.delete(doc.storage_key)
    with pytest.raises(JobError, match="missing from storage") as info:
        await pipeline.process_document(ingest_env.deps, doc.id)
    assert info.value.permanent


async def test_missing_embedding_key_fails_with_the_not_configured_message(
    tmp_path: Path, template_db: Path
) -> None:
    from app.ingestion.embedder import get_embedder

    env = await build_env(tmp_path, template_db)
    try:
        # Same index metadata, but the real gemini provider with no key.
        env.deps.settings = env.settings.model_copy(update={"embedding_provider": "gemini"})
        env.deps.embedder_factory = lambda: get_embedder(env.deps.settings)
        async with env.database.session() as db:
            await chunk_store.write_embedding_config(
                db, chunk_store.EmbeddingConfig("gemini", env.settings.embedding_model, DIM)
            )
            await db.commit()
        doc = await env.add_document(MARKDOWN)
        with pytest.raises(JobError, match="Gemini embedding provider is not configured") as info:
            await pipeline.process_document(env.deps, doc.id)
        assert info.value.permanent is True
        assert "GOOGLE_API_KEY" in info.value.message and "re-index" in info.value.message
    finally:
        await env.close()


async def test_runner_marks_the_document_failed_with_the_readable_error_then_reindex_recovers(
    ingest_env: Env,
) -> None:
    env = ingest_env
    doc = await env.add_document(b"%PDF-1.4 garbage", "broken.pdf", job=JobType.INGEST_DOCUMENT)
    queue = SqliteJobQueue(env.database, lock_timeout_s=600)
    runner = JobRunner(
        queue, build_handlers(env.deps), "t", on_failure=build_failure_hook(env.deps)
    )
    import asyncio

    assert await runner.run_next(asyncio.Event()) is True
    stored = await env.document(doc.id)
    assert stored is not None
    assert stored.status is DocumentStatus.FAILED and "damaged" in (stored.error or "")
    async with env.database.read_session() as db:
        job = (await db.execute(select(Job))).scalar_one()
    assert job.status is JobStatus.FAILED  # permanent: no retries


async def test_transient_failure_requeues_and_shows_the_document_as_queued(ingest_env: Env) -> None:
    import asyncio

    env = ingest_env
    doc = await env.add_document(MARKDOWN, job=JobType.INGEST_DOCUMENT)

    class Flaky(FakeEmbedder):
        async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
            raise EmbeddingError("Service outage.", retryable=True)

    env.deps.embedder_factory = lambda: Flaky(model="fake-hash-v1", dim=DIM)
    queue = SqliteJobQueue(env.database, lock_timeout_s=600)
    runner = JobRunner(
        queue, build_handlers(env.deps), "t", on_failure=build_failure_hook(env.deps)
    )
    await runner.run_next(asyncio.Event())
    stored = await env.document(doc.id)
    assert stored is not None and stored.status is DocumentStatus.UPLOADED
    async with env.database.read_session() as db:
        job = (await db.execute(select(Job))).scalar_one()
    assert (
        job.status is JobStatus.QUEUED and job.attempts == 1 and job.last_error == "Service outage."
    )


# --- re-index ----------------------------------------------------------------------------------


async def test_reindex_replaces_the_chunks_with_the_current_chunking(ingest_env: Env) -> None:
    env = ingest_env
    doc = await env.add_document(MARKDOWN, "sorting.md")
    await pipeline.process_document(env.deps, doc.id)
    old_seqs = {c.seq for c in await env.chunks(doc.id)}
    async with env.database.session() as db:  # simulate stale rows from an older chunker
        for chunk in (await db.execute(select(Chunk))).scalars():
            chunk.text = "stale"
        await db.commit()
    await pipeline.process_document(env.deps, doc.id)
    chunks = await env.chunks(doc.id)
    assert all(c.text != "stale" for c in chunks)
    assert {c.seq for c in chunks}.isdisjoint(old_seqs)  # new rows; seq values are never reused
    assert sorted(r[0] for r in await vector_rows(env)) == sorted(c.seq for c in chunks)


# --- the embedding invariant -------------------------------------------------------------------


async def test_ingest_is_refused_when_the_embedding_settings_differ_from_app_meta(
    ingest_env: Env,
) -> None:
    env = ingest_env
    doc = await env.add_document(MARKDOWN)
    env.deps.settings = env.settings.model_copy(update={"embedding_dim": DIM * 2})
    with pytest.raises(JobError, match="reindex-all") as info:
        await pipeline.process_document(env.deps, doc.id)
    assert info.value.permanent
    assert "fake / fake-hash-v1 / 16 dimensions" in info.value.message
    assert "32 dimensions" in info.value.message
    assert await chunk_count(env.database) == 0
    stored = await env.document(doc.id)
    assert stored is not None and stored.status is DocumentStatus.UPLOADED  # untouched


async def test_reindex_all_recreates_the_vector_table_and_requeues_every_document(
    tmp_path: Path, template_db: Path
) -> None:
    import asyncio

    env = await build_env(tmp_path, template_db)
    try:
        ready = await env.add_document(MARKDOWN, "a.md")
        failed = await env.add_document(b"%PDF- no", "b.pdf", status=DocumentStatus.FAILED)
        deleting = await env.add_document(b"gone", "c.txt", status=DocumentStatus.DELETING)
        await pipeline.process_document(env.deps, ready.id)
        assert len(await vector_rows(env)) == 2

        new_settings = env.settings.model_copy(
            update={"embedding_dim": 8, "embedding_model": "fake-hash-v2"}
        )
        summary = await reindex.reindex_all(env.database, new_settings)
        assert (summary.documents, summary.jobs_enqueued) == (2, 2)  # the deleting one is skipped

        async with env.database.read_session() as db:
            meta = await chunk_store.read_embedding_config(db)
            jobs = (
                (await db.execute(select(Job).where(Job.status == JobStatus.QUEUED)))
                .scalars()
                .all()
            )
            audits = (
                (await db.execute(select(AuditLog).where(AuditLog.action == "reindex_all")))
                .scalars()
                .all()
            )
        assert meta == chunk_store.EmbeddingConfig("fake", "fake-hash-v2", 8)
        assert {j.payload["document_id"] for j in jobs} == {ready.id, failed.id}
        assert all(j.type is JobType.REINDEX_DOCUMENT for j in jobs) and len(audits) == 1
        assert await env.count("chunk_vectors") == 0  # empty and rebuilt
        for doc_id in (ready.id, failed.id):
            row = await env.document(doc_id)
            assert row is not None and row.status is DocumentStatus.UPLOADED and row.error is None
        deleting_row = await env.document(deleting.id)
        assert deleting_row is not None and deleting_row.status is DocumentStatus.DELETING

        # the old settings are now refused, the new ones work and produce 8-dimensional vectors
        with pytest.raises(JobError, match="reindex-all"):
            await pipeline.process_document(env.deps, ready.id)
        new_embedder = FakeEmbedder(model="fake-hash-v2", dim=8)
        deps = pipeline.PipelineDeps(env.database, env.storage, new_settings, lambda: new_embedder)
        queue = SqliteJobQueue(env.database, lock_timeout_s=600)
        runner = JobRunner(queue, build_handlers(deps), "t", on_failure=build_failure_hook(deps))
        while await runner.run_next(asyncio.Event()):
            pass
        row = await env.document(ready.id)
        assert row is not None and row.status is DocumentStatus.READY
        assert sorted(await vector_rows(env)) == [
            (c.seq, 8 * 4) for c in await env.chunks(ready.id)
        ]
        failed_row = await env.document(failed.id)
        assert failed_row is not None and failed_row.status is DocumentStatus.FAILED
        assert all(c.embedding_model == "fake-hash-v2" for c in await env.chunks(ready.id))
    finally:
        await env.close()


async def test_reindex_all_does_not_duplicate_jobs_that_are_already_queued(ingest_env: Env) -> None:
    env = ingest_env
    doc = await env.add_document(MARKDOWN, job=JobType.INGEST_DOCUMENT)
    summary = await reindex.reindex_all(env.database, env.settings)
    assert (summary.documents, summary.jobs_enqueued) == (1, 0)
    async with env.database.read_session() as db:
        jobs = (await db.execute(select(Job))).scalars().all()
    assert len(jobs) == 1 and jobs[0].payload == {"document_id": doc.id}


def test_the_vector_table_dimension_is_really_fixed(tmp_path: Path) -> None:
    """Documents why the invariant exists: a vec0 table rejects vectors of another size."""
    conn = sqlite3.connect(tmp_path / "x.db")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.execute("CREATE VIRTUAL TABLE v USING vec0(embedding float[4] distance_metric=cosine)")
    with pytest.raises(sqlite3.Error):
        conn.execute(
            "INSERT INTO v(rowid, embedding) VALUES (1, ?)",
            (sqlite_vec.serialize_float32([1.0] * 8),),
        )
    conn.close()
