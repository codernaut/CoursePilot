"""Materials pages: pick a module, see its documents, upload (faculty), delete, re-index."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from starlette.responses import RedirectResponse, Response

from app.api.deps import CurrentUser, DbDep, SettingsDep, require_role
from app.core.errors import AppError, not_found
from app.models.enums import Role
from app.services import documents, structure
from app.services.auth import SessionContext
from app.services.file_validation import ALLOWED_EXTENSIONS
from app.web.common import render

router = APIRouter(include_in_schema=False)

FacultyDep = Depends(require_role(Role.FACULTY))

_NOTICES = {
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
) -> Response:
    module = await structure.get_module(db, module_id)
    docs = await documents.list_documents(db, module_id)
    years = await structure.get_tree(db)
    user = request.state.user
    return render(
        request,
        "materials.html",
        {
            "module": module,
            "documents": docs,
            "years": years,
            "can_manage": user.role == Role.FACULTY,
            "max_mb": settings.upload_max_mb,
            "max_bytes": settings.upload_max_mb * 1024 * 1024,
            "extensions": ALLOWED_EXTENSIONS,
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
