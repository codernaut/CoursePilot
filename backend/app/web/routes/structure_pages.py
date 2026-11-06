"""Programme structure page: the Year > Block > Module tree with rename forms."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from starlette.responses import RedirectResponse, Response

from app.api.deps import CurrentUser, DbDep, require_role
from app.core.errors import AppError
from app.models.enums import Role
from app.services import structure
from app.services.auth import SessionContext
from app.web.common import render

router = APIRouter(prefix="/structure", include_in_schema=False)

AdminDep = Depends(require_role(Role.ADMIN))
EditorDep = Depends(require_role(Role.ADMIN, Role.FACULTY))


async def _page(
    request: Request, db: DbDep, *, error: str | None = None, status_code: int = 200
) -> Response:
    years = await structure.get_tree(db)
    return render(
        request,
        "structure.html",
        {
            "years": years,
            "error": error,
            "saved": request.query_params.get("saved") == "1",
        },
        status_code=status_code,
    )


@router.get("")
async def structure_page(request: Request, db: DbDep, _: CurrentUser) -> Response:
    return await _page(request, db)


@router.post("/years/{year_id}")
async def rename_year(
    request: Request,
    year_id: str,
    db: DbDep,
    ctx: SessionContext = AdminDep,
    title: Annotated[str, Form()] = "",
) -> Response:
    try:
        await structure.rename_year(db, ctx.user, year_id, title[:500])
    except AppError as exc:
        return await _page(request, db, error=exc.message, status_code=exc.status_code)
    return RedirectResponse(f"/structure?saved=1#year-{year_id}", status_code=303)


@router.post("/blocks/{block_id}")
async def rename_block(
    request: Request,
    block_id: str,
    db: DbDep,
    ctx: SessionContext = AdminDep,
    title: Annotated[str, Form()] = "",
) -> Response:
    try:
        await structure.rename_block(db, ctx.user, block_id, title[:500])
    except AppError as exc:
        return await _page(request, db, error=exc.message, status_code=exc.status_code)
    return RedirectResponse(f"/structure?saved=1#block-{block_id}", status_code=303)


@router.post("/modules/{module_id}")
async def update_module(
    request: Request,
    module_id: str,
    db: DbDep,
    ctx: SessionContext = EditorDep,
    title: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    web_search_enabled: Annotated[str, Form()] = "",
) -> Response:
    try:
        await structure.update_module(
            db,
            ctx.user,
            module_id,
            title=title[:500],
            description=description[:10000],
            web_search_enabled=web_search_enabled == "on",
        )
    except AppError as exc:
        return await _page(request, db, error=exc.message, status_code=exc.status_code)
    return RedirectResponse(f"/structure?saved=1#module-{module_id}", status_code=303)
