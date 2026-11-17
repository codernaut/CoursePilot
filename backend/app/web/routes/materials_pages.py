"""Materials pages: pick a module, see its documents, upload (faculty), delete, re-index."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from starlette.responses import RedirectResponse, Response

from app.api.deps import CurrentUser, DbDep, SettingsDep, require_role
from app.core.errors import AppError, not_found
from app.models import AccessCode
from app.models.enums import Role
from app.services import access_codes, documents, structure
from app.services.auth import SessionContext
from app.services.file_validation import ALLOWED_EXTENSIONS
from app.web.common import render

router = APIRouter(include_in_schema=False)

FacultyDep = Depends(require_role(Role.FACULTY))

IssuerDep = Depends(require_role(Role.ADMIN, Role.FACULTY))

_NOTICES = {
    "code_revoked": "The access code was revoked. Everyone who joined with it was signed out.",
    "deleting": "The document was hidden and will be deleted shortly.",
    "reindexing": "The document was queued for re-indexing.",
}


async def _page(
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    module_id: str,
    *,
    error: str | None = None,
    status_code: int = 200,
    new_code: tuple[AccessCode, str] | None = None,
) -> Response:
    module = await structure.get_module(db, module_id)
    docs = await documents.list_documents(db, module_id)
    years = await structure.get_tree(db)
    user = request.state.user
    can_share = user.role in (Role.ADMIN, Role.FACULTY)
    return render(
        request,
        "materials.html",
        {
            "module": module,
            "documents": docs,
            "chunk_counts": await documents.chunk_counts(db, docs),
            "years": years,
            "can_manage": user.role == Role.FACULTY,
            "max_mb": settings.upload_max_mb,
            "max_bytes": settings.upload_max_mb * 1024 * 1024,
            "extensions": ALLOWED_EXTENSIONS,
            "can_share": can_share,
            "access_codes": await access_codes.list_codes(db, user, module_id) if can_share else [],
            "durations": access_codes.ALLOWED_DURATIONS,
            "new_code": new_code,
            "error": error,
            "notice": _NOTICES.get(request.query_params.get("notice", "")),
        },
        status_code=status_code,
    )


@router.get("/materials")
async def materials_index(
    request: Request,
    db: DbDep,
    _: CurrentUser,
    module_id: Annotated[str, Query(max_length=64)] = "",
) -> Response:
    """The module picker. ``?module_id=`` (from the picker form) redirects to that module."""
    if module_id:
        module = await structure.get_module(db, module_id)
        return RedirectResponse(f"/modules/{module.id}/materials", status_code=303)
    years = await structure.get_tree(db)
    return render(request, "materials_index.html", {"years": years})


@router.get("/modules/{module_id}/materials")
async def materials_page(
    request: Request, module_id: str, db: DbDep, settings: SettingsDep, _: CurrentUser
) -> Response:
    return await _page(request, db, settings, module_id)


@router.get("/modules/{module_id}/materials/documents")
async def materials_list_fragment(
    request: Request, module_id: str, db: DbDep, _: CurrentUser
) -> Response:
    """The document list on its own: swapped in by HTMX polling and after uploads."""
    module = await structure.get_module(db, module_id)
    docs = await documents.list_documents(db, module_id)
    return render(
        request,
        "_documents.html",
        {
            "module": module,
            "documents": docs,
            "chunk_counts": await documents.chunk_counts(db, docs),
            "can_manage": request.state.user.role == Role.FACULTY,
        },
    )


@router.post("/modules/{module_id}/materials/{document_id}/delete")
async def delete_material(
    request: Request,
    module_id: str,
    document_id: str,
    db: DbDep,
    settings: SettingsDep,
    ctx: SessionContext = FacultyDep,
) -> Response:
    try:
        document = await documents.get_document(db, document_id)
        if document.module_id != module_id:
            raise not_found("Document not found.")
        await documents.request_delete(db, ctx.user, document_id)
    except AppError as exc:
        return await _page(
            request, db, settings, module_id, error=exc.message, status_code=exc.status_code
        )
    return RedirectResponse(f"/modules/{module_id}/materials?notice=deleting", status_code=303)


@router.post("/modules/{module_id}/materials/{document_id}/reindex")
async def reindex_material(
    request: Request,
    module_id: str,
    document_id: str,
    db: DbDep,
    settings: SettingsDep,
    ctx: SessionContext = FacultyDep,
) -> Response:
    try:
        document = await documents.get_document(db, document_id)
        if document.module_id != module_id:
            raise not_found("Document not found.")
        await documents.request_reindex(db, ctx.user, document_id)
    except AppError as exc:
        return await _page(
            request, db, settings, module_id, error=exc.message, status_code=exc.status_code
        )
    return RedirectResponse(f"/modules/{module_id}/materials?notice=reindexing", status_code=303)


@router.post("/modules/{module_id}/access-codes")
async def create_access_code(
    request: Request,
    module_id: str,
    db: DbDep,
    settings: SettingsDep,
    ctx: SessionContext = IssuerDep,
    duration_hours: Annotated[str, Form()] = "",
    label: Annotated[str, Form()] = "",
) -> Response:
    """Create a code and show it, once, on the materials page (it is not stored in plain text)."""
    try:
        hours = int(duration_hours)
    except ValueError:
        hours = 0
    try:
        created = await access_codes.create_code(
            db, ctx.user, module_id, duration_hours=hours, label=label
        )
    except AppError as exc:
        return await _page(
            request, db, settings, module_id, error=exc.message, status_code=exc.status_code
        )
    response = await _page(request, db, settings, module_id, new_code=created)
    response.headers["Cache-Control"] = "no-store"  # the page shows the plain code
    return response


@router.post("/modules/{module_id}/access-codes/{code_id}/revoke")
async def revoke_access_code(
    request: Request,
    module_id: str,
    code_id: str,
    db: DbDep,
    settings: SettingsDep,
    ctx: SessionContext = IssuerDep,
) -> Response:
    try:
        code = await db.get(AccessCode, code_id)
        if code is None or code.module_id != module_id:
            raise not_found("Access code not found.")
        await access_codes.revoke_code(db, ctx.user, code_id)
    except AppError as exc:
        return await _page(
            request, db, settings, module_id, error=exc.message, status_code=exc.status_code
        )
    return RedirectResponse(f"/modules/{module_id}/materials?notice=code_revoked", status_code=303)
