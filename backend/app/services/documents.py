"""Documents: listing, metadata edits, delete and re-index requests, download lookup.

Actual file removal and re-processing happen in the worker (M3); here the API only records the
intent (status + job row) so the request stays short.
"""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import conflict, forbidden, not_found, validation_error
from app.db.chunk_store import count_chunks
from app.jobs import enqueue
from app.models import Document, User
from app.models.enums import DocumentStatus, JobType, Role
from app.services import audit, structure
from app.services.file_validation import strip_unsafe_characters

MAX_TITLE = 300
MAX_WEEK = 60


def require_faculty(actor: User) -> None:
    """Uploading, editing, deleting and re-indexing are faculty only (admins cannot)."""
    if actor.role != Role.FACULTY or not actor.is_active:
        raise forbidden()


async def list_documents(db: AsyncSession, module_id: str) -> list[Document]:
    await structure.get_module(db, module_id)  # 404 for an unknown module
    result = await db.execute(
        select(Document)
        .where(Document.module_id == module_id, Document.status != DocumentStatus.DELETING)
        .order_by(Document.created_at.desc(), Document.title)
    )
    return list(result.scalars().all())


async def chunk_counts(db: AsyncSession, docs: Sequence[Document]) -> dict[str, int]:
    """Indexed chunk count per document id (documents without chunks are absent)."""
    return await count_chunks(db, [d.id for d in docs if d.status == DocumentStatus.READY])


async def get_document(db: AsyncSession, document_id: str) -> Document:
    """A visible document. ``deleting`` documents are hidden, so they are 404."""
    document = await db.get(Document, document_id)
    if document is None or document.status == DocumentStatus.DELETING:
        raise not_found("Document not found.")
    return document


def _squash(text: str) -> str:
    """Whitespace collapsed to single spaces, control/bidi/zero-width characters removed."""
    spaced = "".join(" " if c.isspace() else c for c in text)
    return " ".join(strip_unsafe_characters(spaced).split())


def _clean_title(title: str) -> str:
    cleaned = _squash(title)
    if not cleaned or len(cleaned) > MAX_TITLE:
        raise validation_error(f"Title must be 1-{MAX_TITLE} characters.")
    return cleaned


def _clean_week(week: str) -> str | None:
    cleaned = _squash(week)
    if len(cleaned) > MAX_WEEK:
        raise validation_error(f"Week must be at most {MAX_WEEK} characters.")
    return cleaned or None


async def update_document(
    db: AsyncSession,
    actor: User,
    document_id: str,
    *,
    title: str | None = None,
    week: str | None = None,
) -> Document:
    require_faculty(actor)
    document = await get_document(db, document_id)
    details: dict[str, object] = {}
    if title is not None:
        new_title = _clean_title(title)
        if new_title != document.title:
            details["title"] = {"from": document.title, "to": new_title}
            document.title = new_title
    if week is not None:
        new_week = _clean_week(week)
        if new_week != document.week:
            details["week"] = {"from": document.week, "to": new_week}
            document.week = new_week
    if details:
        audit.record(
            db,
            "document_updated",
            actor_id=actor.id,
            target_type="document",
            target_id=document.id,
            details=details,
        )
    await db.commit()
    return document


async def request_delete(db: AsyncSession, actor: User, document_id: str) -> Document:
    """Mark the document ``deleting`` (hidden from lists, search and download) and enqueue the
    ``delete_document`` job that removes chunks, the stored file and the row."""
    require_faculty(actor)
    document = await get_document(db, document_id)
    document.status = DocumentStatus.DELETING
    enqueue(db, JobType.DELETE_DOCUMENT, {"document_id": document.id})
    audit.record(
        db,
        "document_delete_requested",
        actor_id=actor.id,
        target_type="document",
        target_id=document.id,
        details={"module_id": document.module_id, "filename": document.original_filename},
    )
    await db.commit()
    return document


async def request_reindex(db: AsyncSession, actor: User, document_id: str) -> Document:
    """Queue a ``reindex_document`` job. Only ``ready`` or ``failed`` documents qualify, and a
    ``ready`` one that is already re-indexing does not: it already has work pending.

    A ``ready`` document stays ``ready`` (and searchable: its old chunks are replaced only when the
    new ones are done) and is flagged ``reindexing``. A ``failed`` one goes back to ``uploaded`` so
    the page shows (and polls) the pending work."""
    require_faculty(actor)
    document = await get_document(db, document_id)
    if document.status not in (DocumentStatus.READY, DocumentStatus.FAILED) or document.reindexing:
        raise conflict("This document is still being processed; try again when it has finished.")
    _mark_reindex_pending(document)
    enqueue(db, JobType.REINDEX_DOCUMENT, {"document_id": document.id})
    audit.record(
        db,
        "document_reindex_requested",
        actor_id=actor.id,
        target_type="document",
        target_id=document.id,
        details={"module_id": document.module_id},
    )
    await db.commit()
    return document


def _mark_reindex_pending(document: Document) -> None:
    """Show pending (re-)processing work on the document without making it unsearchable."""
    document.error = None
    if document.status == DocumentStatus.READY:
        document.reindexing = True
    else:
        document.status = DocumentStatus.UPLOADED


async def mark_job_requeued(db: AsyncSession, document_id: object) -> None:
    """An administrator re-queued a failed document job: show the pending work again (same rules
    as ``request_reindex``). Deleting or vanished documents are left alone."""
    if not isinstance(document_id, str):
        return
    document = await db.get(Document, document_id)
    if document is None or document.status in (DocumentStatus.DELETING, DocumentStatus.PROCESSING):
        return
    if document.status in (DocumentStatus.READY, DocumentStatus.FAILED, DocumentStatus.UPLOADED):
        _mark_reindex_pending(document)
