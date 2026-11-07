"""``reindex-all``: rebuild the vector index after the embedding provider, model or dimension
changed (CLAUDE.md section 7.4). Chunks stay in place; the vector table is recreated empty with the
configured dimension, ``app_meta`` is updated, and every document goes back to ``uploaded`` with a
``reindex_document`` job, so the worker re-embeds them one by one (documents are not searchable
until they are ``ready`` again)."""

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select

from app.core.config import Settings
from app.db import chunk_store
from app.db.base import utcnow
from app.db.session import Database
from app.ingestion.index_meta import config_from_settings
from app.jobs import enqueue
from app.models import Document, Job
from app.models.enums import DocumentStatus, JobStatus, JobType
from app.services import audit

_DOCUMENT_JOB_TYPES = (JobType.INGEST_DOCUMENT, JobType.REINDEX_DOCUMENT)


class WorkerBusyError(Exception):
    """A worker is processing a job right now (its heartbeat is fresh)."""


@dataclass(frozen=True)
class ReindexSummary:
    documents: int
    jobs_enqueued: int


async def count_documents(database: Database) -> int:
    async with database.read_session() as db:
        result = await db.execute(
            select(func.count())
            .select_from(Document)
            .where(Document.status != DocumentStatus.DELETING)
        )
        return int(result.scalar_one())


async def reindex_all(database: Database, settings: Settings) -> ReindexSummary:
    """Recreate ``chunk_vectors`` with the configured dimension and queue every document."""
    config = config_from_settings(settings)
    stale_before = utcnow() - timedelta(
        seconds=min(settings.heartbeat_stale_s, settings.job_lock_timeout_s)
    )
    async with database.session() as db:
        busy = (
            await db.execute(
                select(func.count())
                .select_from(Job)
                .where(Job.status == JobStatus.RUNNING, Job.locked_at >= stale_before)
            )
        ).scalar_one()
        if busy:
            raise WorkerBusyError
        await chunk_store.recreate_vector_table(db, config.dim)
        await chunk_store.write_embedding_config(db, config)

        queued_rows = (
            await db.execute(
                select(Job.payload).where(
                    Job.type.in_(_DOCUMENT_JOB_TYPES), Job.status == JobStatus.QUEUED
                )
            )
        ).scalars()
        already_queued = {p.get("document_id") for p in queued_rows}

        documents = (
            (await db.execute(select(Document).where(Document.status != DocumentStatus.DELETING)))
            .scalars()
            .all()
        )
        enqueued = 0
        for document in documents:
            document.status = DocumentStatus.UPLOADED
            document.reindexing = False  # not searchable now; the whole document is pending
            document.error = None
            if document.id not in already_queued:
                enqueue(db, JobType.REINDEX_DOCUMENT, {"document_id": document.id})
                enqueued += 1
        audit.record(
            db,
            "reindex_all",
            actor_id=None,
            details={
                "provider": config.provider,
                "model": config.model,
                "dim": config.dim,
                "documents": len(documents),
            },
        )
        await db.commit()
    return ReindexSummary(documents=len(documents), jobs_enqueued=enqueued)
