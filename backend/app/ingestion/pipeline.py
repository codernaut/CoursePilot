"""Document processing pipeline: download, parse, chunk, embed, then replace the document's index
rows in ONE write transaction (CLAUDE.md sections 6.2 and 7.3).

* Nothing slow runs inside a database transaction: reading the file, parsing, chunking and the
  embedding calls all happen first; the write transaction only deletes the old rows and inserts the
  new ones.
* Idempotent: running it twice leaves the same chunks. A failure before the write transaction (or
  inside it) leaves the previous chunks untouched.
* A document the API marked ``deleting`` in the meantime is never made ``ready`` again: the
  results are discarded and the delete job removes the rest.
* CPU-heavy steps (parsing, chunking) run in a worker thread, never on the event loop.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePath

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.db import chunk_store
from app.db.session import Database
from app.ingestion.chunker import DocChunk, chunk_document
from app.ingestion.embedder import Embedder, EmbeddingError
from app.ingestion.index_meta import ensure_index_matches
from app.ingestion.parsers import ParseError, ParseLimitError, ParseLimits, parse_document
from app.jobs.base import JobError
from app.models import AuditLog, Chunk, Document, Module
from app.models.enums import DocumentStatus
from app.storage import Storage, StorageError, StorageNotFoundError

log = get_logger(__name__)

MAX_CHUNKS_PER_DOCUMENT = 5000
MAX_ERROR_LENGTH = 1000
_MB = 1024 * 1024


class Outcome(StrEnum):
    READY = "ready"
    DISCARDED = "discarded"  # the document was deleted (or is being deleted) meanwhile


@dataclass
class PipelineDeps:
    database: Database
    storage: Storage
    settings: Settings
    embedder_factory: Callable[[], Embedder]


@dataclass(frozen=True)
class _DocInfo:
    id: str
    title: str
    filename: str
    storage_key: str
    module_id: str
    block_id: str
    year_id: str
    programme_path: str


def _limits(settings: Settings) -> ParseLimits:
    return ParseLimits(
        max_pages=settings.parse_max_pages,
        timeout_s=float(settings.parse_timeout_s),
        ocr_enabled=settings.ocr_enabled,
    )


async def _read_file(storage: Storage, key: str, max_bytes: int) -> bytes:
    data = bytearray()
    try:
        async for chunk in storage.read(key):
            data += chunk
            if len(data) > max_bytes:
                raise ParseLimitError("The stored file is larger than the upload limit.")
    except StorageNotFoundError as exc:
        raise ParseError(
            "The uploaded file is missing from storage. Delete this document and upload it again."
        ) from exc
    return bytes(data)


def _to_chunk_rows(info: _DocInfo, chunks: list[DocChunk], model: str) -> list[Chunk]:
    return [
        Chunk(
            document_id=info.id,
            module_id=info.module_id,
            block_id=info.block_id,
            year_id=info.year_id,
            ordinal=c.ordinal,
            text=c.text,
            embed_text=c.embed_text,
            token_count=c.token_count,
            page_start=c.page_start,
            page_end=c.page_end,
            slide_number=c.slide_number,
            section_path=c.section_path,
            embedding_model=model,
            meta=c.metadata,
        )
        for c in chunks
    ]


async def _load_info(db: AsyncSession, document_id: str) -> _DocInfo | None:
    document = await db.get(Document, document_id)
    if document is None or document.status == DocumentStatus.DELETING:
        return None
    module = await db.get(Module, document.module_id)
    if module is None:  # cannot happen (foreign key), but never crash on it
        return None
    block = module.block
    year = block.year
    return _DocInfo(
        id=document.id,
        title=document.title,
        filename=document.original_filename,
        storage_key=document.storage_key,
        module_id=module.id,
        block_id=block.id,
        year_id=year.id,
        programme_path=f"{year.title} > {block.title} > {module.title}",
    )


def _as_job_error(exc: Exception) -> JobError:
    """Translate pipeline failures into readable job errors (permanent unless a retry can help)."""
    if isinstance(exc, JobError):
        return exc
    if isinstance(exc, ParseError):
        return JobError(str(exc), permanent=True)
    if isinstance(exc, EmbeddingError):
        return JobError(exc.message, permanent=not exc.retryable)
    if isinstance(exc, AppError):  # index_mismatch and friends
        return JobError(exc.message, permanent=True)
    if isinstance(exc, StorageError):
        return JobError("The uploaded file could not be read from storage.", permanent=False)
    raise exc


async def process_document(deps: PipelineDeps, document_id: str) -> Outcome:
    """Ingest or re-index one document. Raises ``JobError`` with a readable message on failure."""
    try:
        return await _process(deps, document_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise _as_job_error(exc) from exc


async def _process(deps: PipelineDeps, document_id: str) -> Outcome:
    settings = deps.settings
    async with deps.database.session() as db:
        await ensure_index_matches(db, settings)
        info = await _load_info(db, document_id)
        if info is None:
            log.info("document_gone_before_processing", document_id=document_id)
            return Outcome.DISCARDED
        document = await db.get(Document, document_id)
        if document is None:
            return Outcome.DISCARDED
        document.status = DocumentStatus.PROCESSING
        document.error = None
        await db.commit()

    embedder = deps.embedder_factory()  # raises EmbeddingNotConfiguredError without a key
    try:
        data = await _read_file(deps.storage, info.storage_key, settings.upload_max_mb * _MB + 1)
        parsed = await asyncio.to_thread(
            parse_document,
            PurePath(info.filename).suffix.lstrip(".").lower(),
            data,
            _limits(settings),
        )
        del data
        chunks = await asyncio.to_thread(
            chunk_document,
            parsed.sections,
            document_title=info.title,
            programme_path=info.programme_path,
        )
        if not chunks:
            raise ParseError("No readable text was found in this file.")
        if len(chunks) > MAX_CHUNKS_PER_DOCUMENT:
            raise ParseLimitError(
                f"This file produces {len(chunks)} text chunks; the limit is "
                f"{MAX_CHUNKS_PER_DOCUMENT}. Split it into smaller files."
            )
        vectors = await embedder.embed_documents([c.embed_text for c in chunks])
    finally:
        await embedder.aclose()

    async with deps.database.session() as db:
        await ensure_index_matches(db, settings)  # reindex-all may have run meanwhile
        document = await db.get(Document, document_id)
        if document is None or document.status == DocumentStatus.DELETING:
            log.info("document_deleted_during_processing", document_id=document_id)
            return Outcome.DISCARDED
        await chunk_store.delete_document_index(db, document_id)
        await chunk_store.insert_chunks(db, _to_chunk_rows(info, chunks, embedder.model), vectors)
        document.status = DocumentStatus.READY
        document.error = None
        document.page_count = parsed.page_count
        await db.commit()
    log.info("document_processed", document_id=document_id, chunks=len(chunks))
    return Outcome.READY


async def delete_document(deps: PipelineDeps, document_id: str) -> bool:
    """The ``delete_document`` job: stored file, index rows, then the document row (audited).
    Only acts on a document the API marked ``deleting``. Returns whether a document was removed."""
    async with deps.database.read_session() as db:
        document = await db.get(Document, document_id)
        if document is None or document.status != DocumentStatus.DELETING:
            return False
        key = document.storage_key
    await deps.storage.delete(key)  # idempotent; done first so a retry can finish the job
    async with deps.database.session() as db:
        document = await db.get(Document, document_id)
        if document is None or document.status != DocumentStatus.DELETING:
            return False
        removed = await chunk_store.delete_document_index(db, document_id)
        db.add(
            AuditLog(
                actor_id=None,
                action="document_deleted",
                target_type="document",
                target_id=document.id,
                details={
                    "module_id": document.module_id,
                    "filename": document.original_filename,
                    "chunks_removed": removed,
                },
            )
        )
        await db.delete(document)
        await db.commit()
    log.info("document_deleted", document_id=document_id)
    return True


async def mark_failed(database: Database, document_id: str, message: str) -> None:
    """Show a failure on the document (unless it is being deleted or is gone)."""
    async with database.session() as db:
        document = await db.get(Document, document_id)
        if document is None or document.status == DocumentStatus.DELETING:
            return
        document.status = DocumentStatus.FAILED
        document.error = message[:MAX_ERROR_LENGTH]
        await db.commit()


async def mark_queued(database: Database, document_id: str) -> None:
    """Back to ``uploaded`` (shown as Queued) after a retry was scheduled or a shutdown."""
    async with database.session() as db:
        document = await db.get(Document, document_id)
        if document is None or document.status in (
            DocumentStatus.DELETING,
            DocumentStatus.READY,
            DocumentStatus.FAILED,
        ):
            return
        document.status = DocumentStatus.UPLOADED
        await db.commit()
