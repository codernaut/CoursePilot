"""Streaming document upload (CLAUDE.md 6.2 and 13).

The request body is parsed as it arrives (python-multipart's incremental parser). Each file part
is streamed to ``Storage`` while its sha256 and size are computed and its content is checked, so
nothing is buffered in memory and ``UPLOAD_MAX_MB`` is enforced during streaming, not from
``Content-Length``.

Per-file results: a request may carry several files. Each file is accepted or rejected on its
own (unsupported type, content mismatch, empty, duplicate) and the rest still go through. A file
over the size limit ends the request: it is rejected with 413, files before it are kept, and the
remaining body is not read. Whatever was stored for a rejected or failed file is deleted again.

Nothing here imports FastAPI.
"""

import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from python_multipart.multipart import MultipartParser, parse_options_header
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError, forbidden, validation_error
from app.core.logging import get_logger
from app.jobs import enqueue
from app.models import Document, User
from app.models.enums import DocumentStatus, JobType, Role
from app.services import audit, structure
from app.services.documents import require_faculty
from app.services.file_validation import (
    ALLOWED_EXTENSIONS,
    ALLOWED_TYPES,
    ContentChecker,
    ContentError,
    FileType,
    check_stored_zip,
    file_extension,
    sanitize_filename,
    title_from_filename,
)
from app.storage import Storage, StorageWriter, new_key

log = get_logger(__name__)

MAX_FILES_PER_REQUEST = 10
MAX_PARTS_PER_REQUEST = 40
_BODY_OVERHEAD_BYTES = 1024 * 1024


def max_request_bytes(settings: Settings) -> int:
    """Upper bound for a whole upload request body (all files plus multipart framing)."""
    return settings.upload_max_mb * 1024 * 1024 * MAX_FILES_PER_REQUEST + _BODY_OVERHEAD_BYTES


@dataclass
class RejectedFile:
    filename: str
    code: str
    message: str
    status_code: int
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class UploadResult:
    accepted: list[Document] = field(default_factory=list)
    rejected: list[RejectedFile] = field(default_factory=list)


class _OversizeError(Exception):
    def __init__(self, filename: str) -> None:
        super().__init__(filename)
        self.filename = filename


class _MultipartEvents:
    """Turns python-multipart's callbacks into an ordered event list for the async consumer."""

    def __init__(self, boundary: bytes, max_size: int) -> None:
        self.events: list[tuple[str, Any]] = []
        self._headers: dict[str, str] = {}
        self._name = bytearray()
        self._value = bytearray()
        self._parser = MultipartParser(
            boundary,
            callbacks={
                "on_part_begin": self._part_begin,
                "on_header_field": self._header_field,
                "on_header_value": self._header_value,
                "on_header_end": self._header_end,
                "on_headers_finished": self._headers_finished,
                "on_part_data": self._part_data,
                "on_part_end": self._part_end,
            },
            max_size=max_size,
            max_header_count=20,
            max_header_size=4096,
        )

    def _part_begin(self) -> None:
        self._headers = {}

    def _header_field(self, data: bytes, start: int, end: int) -> None:
        self._name += data[start:end]

    def _header_value(self, data: bytes, start: int, end: int) -> None:
        self._value += data[start:end]

    def _header_end(self) -> None:
        self._headers[self._name.decode("latin-1").lower()] = self._value.decode("latin-1")
        self._name.clear()
        self._value.clear()

    def _headers_finished(self) -> None:
        self.events.append(("headers", dict(self._headers)))

    def _part_data(self, data: bytes, start: int, end: int) -> None:
        self.events.append(("data", bytes(data[start:end])))

    def _part_end(self) -> None:
        self.events.append(("end", None))

    def feed(self, chunk: bytes) -> list[tuple[str, Any]]:
        self.events = []
        self._parser.write(chunk)
        return self.events

    def finalize(self) -> None:
        self._parser.finalize()


@dataclass
class _Part:
    """State of the multipart part currently being read."""

    mode: str  # "ignore" (form field), "skip" (rejected file, drain) or "store"
    filename: str = ""
    file_type: FileType | None = None
    key: str = ""
    writer: StorageWriter | None = None
    checker: ContentChecker | None = None
    hasher: Any = None
    size: int = 0
    committed: bool = False  # object visible in storage
    registered: bool = False  # documents row committed

    def store_parts(self) -> tuple[StorageWriter, ContentChecker, FileType]:
        """The writer, checker and type of a ``store`` part (always set for those)."""
        if self.writer is None or self.checker is None or self.file_type is None:
            raise RuntimeError("part is not being stored")
        return self.writer, self.checker, self.file_type


def _boundary(content_type: str | None) -> bytes:
    value, params = parse_options_header(content_type)
    boundary = params.get(b"boundary")
    if value != b"multipart/form-data" or not boundary:
        raise validation_error("Send the file as multipart/form-data.")
    return boundary


def _file_params(content_disposition: str) -> tuple[bool, str]:
    """(is a file part, raw filename) from a Content-Disposition header (latin-1 decoded)."""
    _, params = parse_options_header(content_disposition)
    raw = params.get(b"filename")
    if raw is None:
        return False, ""
    return True, raw.decode("utf-8", errors="replace")


class _UploadReceiver:
    def __init__(
        self,
        db: AsyncSession,
        storage: Storage,
        settings: Settings,
        actor: User,
        module_id: str,
    ) -> None:
        self.db = db
        self.storage = storage
        self.max_file_bytes = settings.upload_max_mb * 1024 * 1024
        self.actor = actor
        self.module_id = module_id
        self.result = UploadResult()
        self.part: _Part | None = None
        self.file_count = 0
        self.part_count = 0

    # --- part lifecycle ---------------------------------------------------------------------

    def reject(
        self, filename: str, code: str, message: str, status_code: int, **details: Any
    ) -> None:
        self.result.rejected.append(RejectedFile(filename, code, message, status_code, details))

    async def begin(self, headers: dict[str, str]) -> None:
        self.part_count += 1
        if self.part_count > MAX_PARTS_PER_REQUEST:
            raise validation_error("Too many parts in the request.")
        is_file, raw_name = _file_params(headers.get("content-disposition", ""))
        if not is_file:
            self.part = _Part(mode="ignore")
            return
        filename = sanitize_filename(raw_name)
        self.file_count += 1
        if self.file_count > MAX_FILES_PER_REQUEST:
            self.reject(
                filename,
                "too_many_files",
                f"At most {MAX_FILES_PER_REQUEST} files per request.",
                422,
            )
            self.part = _Part(mode="skip", filename=filename)
            return
        extension = file_extension(filename)
        file_type = ALLOWED_TYPES.get(extension)
        if file_type is None:
            self.reject(
                filename,
                "unsupported_file_type",
                "Unsupported file type. Allowed: " + ", ".join(ALLOWED_EXTENSIONS) + ".",
                415,
            )
            self.part = _Part(mode="skip", filename=filename)
            return
        key = new_key()
        part = _Part(
            mode="store",
            filename=filename,
            file_type=file_type,
            key=key,
            checker=ContentChecker(file_type),
            hasher=hashlib.sha256(),
        )
        part.writer = await self.storage.open_writer(key)
        self.part = part

    async def data(self, chunk: bytes) -> None:
        part = self.part
        if part is None or part.mode != "store":
            return
        writer, checker, _ = part.store_parts()
        part.size += len(chunk)
        if part.size > self.max_file_bytes:
            raise _OversizeError(part.filename)
        try:
            checker.feed(chunk)
        except ContentError as exc:
            await self._fail_part(part, "unsupported_file_type", str(exc), 415)
            return
        part.hasher.update(chunk)
        await writer.write(chunk)

    async def end(self) -> None:
        part = self.part
        if part is None or part.mode != "store":
            self.part = None
            return
        writer, checker, file_type = part.store_parts()
        try:
            checker.finish()
            if part.size == 0:
                await self._fail_part(part, "validation_error", "The file is empty.", 422)
                return
            await writer.commit()
            part.committed = True
            if file_type.kind in ("pptx", "docx"):
                await check_stored_zip(self.storage, part.key, file_type)
        except ContentError as exc:
            await self._fail_part(part, "unsupported_file_type", str(exc), 415)
            return
        except BaseException:
            # Any other failure (I/O error, cancellation) must not leave the object behind.
            self.part = None
            await self._discard(part)
            raise
        self.part = None
        await self._register(part)

    async def _fail_part(self, part: _Part, code: str, message: str, status_code: int) -> None:
        """Reject the file, discard what was stored for it and drain the rest of the part."""
        self.reject(part.filename, code, message, status_code)
        await self._discard(part)
        part.mode = "skip"
        self.part = part

    async def _discard(self, part: _Part) -> None:
        """Remove a part's partial or committed object (never touches registered documents)."""
        if part.registered:
            return
        if part.writer is not None and not part.committed:
            await part.writer.abort()
        if part.committed:
            await self.storage.delete(part.key)
            part.committed = False
        part.writer = None

    async def _register(self, part: _Part) -> None:
        """Insert the documents row, the ingest job and the audit entry in one transaction."""
        _, _, file_type = part.store_parts()
        sha256 = part.hasher.hexdigest()
        await self._require_still_authorized(part)
        existing = (
            await self.db.execute(
                select(Document.id, Document.status).where(
                    Document.module_id == self.module_id, Document.content_sha256 == sha256
                )
            )
        ).first()
        if existing is not None:
            await self.db.commit()  # nothing to save; ends the transaction (releases the lock)
            await self._discard(part)
            self._duplicate(part.filename, existing[0], existing[1])
            return
        document = Document(
            module_id=self.module_id,
            uploaded_by=self.actor.id,
            title=title_from_filename(part.filename),
            original_filename=part.filename,
            mime_type=file_type.mime_type,
            size_bytes=part.size,
            storage_key=part.key,
            content_sha256=sha256,
            status=DocumentStatus.UPLOADED,
        )
        try:
            self.db.add(document)
            await self.db.flush()
            enqueue(self.db, JobType.INGEST_DOCUMENT, {"document_id": document.id})
            audit.record(
                self.db,
                "document_uploaded",
                actor_id=self.actor.id,
                target_type="document",
                target_id=document.id,
                details={
                    "module_id": self.module_id,
                    "filename": part.filename,
                    "size_bytes": part.size,
                    "sha256": sha256,
                },
            )
            await self.db.commit()
        except IntegrityError:
            # Lost a race against an identical upload to the same module.
            await self.db.rollback()
            await self._discard(part)
            found = (
                await self.db.execute(
                    select(Document.id, Document.status).where(
                        Document.module_id == self.module_id, Document.content_sha256 == sha256
                    )
                )
            ).first()
            self._duplicate(part.filename, found[0] if found else None, found[1] if found else None)
            await self.db.rollback()
            return
        except BaseException:
            await self.db.rollback()
            await self._discard(part)
            raise
        part.registered = True
        # Detach it: a later rollback (duplicate or error on another file) must not expire it.
        self.db.expunge(document)
        self.result.accepted.append(document)

    async def _require_still_authorized(self, part: _Part) -> None:
        """The auth transaction was committed before streaming started, so a user deactivated
        (or demoted) while the body was arriving is caught here, inside the registering
        transaction."""
        row = (
            await self.db.execute(select(User.is_active, User.role).where(User.id == self.actor.id))
        ).first()
        if row is None or not row[0] or row[1] != Role.FACULTY:
            await self.db.rollback()
            await self._discard(part)
            raise forbidden()

    def _duplicate(
        self, filename: str, existing_id: str | None, existing_status: DocumentStatus | None
    ) -> None:
        message = "This file has already been uploaded to this module."
        if existing_status == DocumentStatus.DELETING:
            message = (
                "An identical file in this module is being deleted; try again in a few moments."
            )
        self.reject(
            filename,
            "duplicate_document",
            message,
            409,
            existing_document_id=existing_id,
        )

    async def cleanup(self) -> None:
        """Called when the request ends for any reason: drop an unfinished part's object."""
        part, self.part = self.part, None
        if part is not None and part.mode == "store":
            await self._discard(part)


async def receive_documents(
    db: AsyncSession,
    storage: Storage,
    settings: Settings,
    actor: User,
    module_id: str,
    *,
    content_type: str | None,
    content_length: int | None,
    stream: AsyncIterator[bytes],
) -> UploadResult:
    """Authorize, then stream-parse the multipart body and store every acceptable file."""
    require_faculty(actor)
    await structure.get_module(db, module_id)  # 404 before reading the body
    boundary = _boundary(content_type)
    limit = max_request_bytes(settings)
    if content_length is not None and content_length > limit:
        raise AppError("payload_too_large", "The upload is too large.", status_code=413)
    # Release the write lock taken by authentication: a slow upload must not block other writers.
    await db.commit()

    receiver = _UploadReceiver(db, storage, settings, actor, module_id)
    events = _MultipartEvents(boundary, limit)
    total = 0
    try:
        async for chunk in stream:
            total += len(chunk)
            if total > limit:
                raise AppError("payload_too_large", "The upload is too large.", status_code=413)
            try:
                parsed = events.feed(chunk)
            except Exception as exc:
                raise AppError(
                    "bad_request", "The upload body is not valid multipart data.", status_code=400
                ) from exc
            for kind, payload in parsed:
                if kind == "headers":
                    await receiver.begin(payload)
                elif kind == "data":
                    await receiver.data(payload)
                else:
                    await receiver.end()
        try:
            events.finalize()
        except Exception as exc:
            raise AppError(
                "bad_request", "The upload body is not valid multipart data.", status_code=400
            ) from exc
    except _OversizeError as exc:
        receiver.reject(
            exc.filename,
            "payload_too_large",
            f"The file is larger than the {settings.upload_max_mb} MB limit.",
            413,
            max_mb=settings.upload_max_mb,
        )
    finally:
        await receiver.cleanup()

    result = receiver.result
    if not result.accepted and not result.rejected:
        raise validation_error("No file was uploaded.")
    return result
