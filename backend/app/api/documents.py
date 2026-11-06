"""Documents JSON API: upload, list, read, edit, delete, re-index and download."""

import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.deps import CurrentUser, DbDep, SettingsDep, StorageDep, require_role
from app.core.errors import AppError, not_found
from app.models import Document
from app.models.enums import Role
from app.schemas.documents import (
    DocumentOut,
    DocumentPatch,
    UploadErrorOut,
    UploadResponse,
)
from app.services import documents, uploads
from app.services.auth import SessionContext

router = APIRouter(tags=["documents"])

FacultyDep = Depends(require_role(Role.FACULTY))

# Every download is inert: `sandbox` gives the document an opaque origin without scripts. The one
# exception is inline PDF: Chromium's built-in PDF viewer does not render in a sandboxed
# document, so a PDF gets a policy that forbids everything instead (and nosniff + a
# server-detected Content-Type, so it cannot be reinterpreted as HTML).
DOWNLOAD_CSP = "sandbox"
INLINE_PDF_CSP = (
    "default-src 'none'; object-src 'none'; script-src 'none'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
_INLINE_MIME_TYPES = frozenset({"application/pdf"})
_ASCII_UNSAFE = re.compile(r"[^A-Za-z0-9 ._()\-+,;=@\[\]]")


def content_disposition(disposition: str, filename: str) -> str:
    """RFC 6266 header: an ASCII fallback ``filename`` plus the UTF-8 ``filename*`` (RFC 5987)."""
    fallback = _ASCII_UNSAFE.sub("_", filename) or "download"
    return f"{disposition}; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


def _upload_error(rejected: uploads.RejectedFile) -> UploadErrorOut:
    return UploadErrorOut(
        filename=rejected.filename,
        code=rejected.code,
        message=rejected.message,
        status=rejected.status_code,
        details=rejected.details,
    )


@router.post(
    "/modules/{module_id}/documents",
    response_model=UploadResponse,
    status_code=202,
    responses={
        409: {"description": "Duplicate document (nothing was accepted)"},
        413: {"description": "File too large"},
        415: {"description": "Unsupported or mismatching file type"},
    },
)
async def upload_documents(
    module_id: str,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    storage: StorageDep,
    ctx: SessionContext = FacultyDep,
) -> JSONResponse:
    """Upload one or more files (multipart/form-data, file parts). Each file gets its own
    result: 202 with ``documents`` (accepted, queued for processing) and ``errors`` when at
    least one file was accepted, otherwise the first file's error status (409, 413, 415, 422)."""
    declared = request.headers.get("content-length")
    result = await uploads.receive_documents(
        db,
        storage,
        settings,
        ctx.user,
        module_id,
        content_type=request.headers.get("content-type"),
        content_length=int(declared) if declared and declared.isdigit() else None,
        stream=request.stream(),
    )
    errors = [_upload_error(r) for r in result.rejected]
    if not result.accepted:
        first = result.rejected[0]
        raise AppError(
            first.code,
            first.message,
            status_code=first.status_code,
            details={**first.details, "errors": [e.model_dump() for e in errors]},
        )
    body = UploadResponse(
        documents=[DocumentOut.model_validate(d) for d in result.accepted], errors=errors
    )
    return JSONResponse(status_code=202, content=body.model_dump(mode="json"))


@router.get("/modules/{module_id}/documents", response_model=list[DocumentOut])
async def list_module_documents(module_id: str, db: DbDep, _: CurrentUser) -> list[DocumentOut]:
    return [DocumentOut.model_validate(d) for d in await documents.list_documents(db, module_id)]


@router.get("/documents/{document_id}", response_model=DocumentOut)
async def get_document(document_id: str, db: DbDep, _: CurrentUser) -> DocumentOut:
    return DocumentOut.model_validate(await documents.get_document(db, document_id))


@router.patch("/documents/{document_id}", response_model=DocumentOut)
async def patch_document(
    document_id: str, body: DocumentPatch, db: DbDep, ctx: SessionContext = FacultyDep
) -> DocumentOut:
    document = await documents.update_document(
        db, ctx.user, document_id, title=body.title, week=body.week
    )
    return DocumentOut.model_validate(document)


@router.delete("/documents/{document_id}", response_model=DocumentOut, status_code=202)
async def delete_document(
    document_id: str, db: DbDep, ctx: SessionContext = FacultyDep
) -> DocumentOut:
    """Hides the document immediately and queues the job that removes it for good."""
    return DocumentOut.model_validate(await documents.request_delete(db, ctx.user, document_id))


@router.post("/documents/{document_id}/reindex", response_model=DocumentOut, status_code=202)
async def reindex_document(
    document_id: str, db: DbDep, ctx: SessionContext = FacultyDep
) -> DocumentOut:
    return DocumentOut.model_validate(await documents.request_reindex(db, ctx.user, document_id))


@router.get("/documents/{document_id}/download")
async def download_document(
    document_id: str, db: DbDep, storage: StorageDep, _: CurrentUser
) -> StreamingResponse:
    document: Document = await documents.get_document(db, document_id)
    if not await storage.exists(document.storage_key):
        raise not_found("The stored file is missing.")
    mime, size, key = document.mime_type, document.size_bytes, document.storage_key
    disposition = "inline" if mime in _INLINE_MIME_TYPES else "attachment"
    headers = {
        "Content-Disposition": content_disposition(disposition, document.original_filename),
        "Content-Length": str(size),
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": INLINE_PDF_CSP if disposition == "inline" else DOWNLOAD_CSP,
    }
    await db.rollback()  # do not hold a read snapshot while the file streams
    return StreamingResponse(storage.read(key), media_type=mime, headers=headers)
