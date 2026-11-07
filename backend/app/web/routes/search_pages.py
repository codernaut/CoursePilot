"""Search page: ask the material a question and open the matching passages in a side panel."""

from typing import Annotated

from fastapi import APIRouter, Query, Request
from starlette.responses import Response

from app.api.deps import CurrentUser, DbDep, SettingsDep
from app.core.errors import AppError
from app.models.enums import Role
from app.retrieval.access import AccessContext
from app.retrieval.hybrid import SearchFilters
from app.services import search as search_service
from app.services import structure
from app.web.common import render

router = APIRouter(include_in_schema=False)


@router.get("/search")
async def search_page(
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    ctx: CurrentUser,
    q: Annotated[str, Query(max_length=2000)] = "",
    module_id: Annotated[str, Query(max_length=64)] = "",
    scope: Annotated[str, Query(max_length=16)] = "",
    week: Annotated[str, Query(max_length=60)] = "",
    widen: Annotated[str, Query(max_length=8)] = "",
) -> Response:
    context: dict[str, object] = {
        "years": await structure.get_tree(db),
        "q": q,
        "module_id": module_id,
        "scope": scope or ("module" if module_id else "all"),
        "week": week,
        "widen": widen == "1",
        "searched": False,
        "tiers": [],
        "hits": [],
        "error": None,
    }
    status_code = 200
    if q.strip():
        context["searched"] = True
        try:
            search_scope = await search_service.resolve_scope(
                db, scope=scope or None, module_id=module_id or None
            )
            outcome = await search_service.search(
                db,
                settings=settings,
                embedder=request.app.state.query_embedder,
                access=AccessContext.from_user(ctx.user),
                query=q,
                scope=search_scope,
                filters=SearchFilters(week=week.strip() or None),
                widen=widen == "1",
            )
            context["tiers"] = outcome.tiers
            context["hits"] = [
                {"chunk": c, "snippet": search_service.snippet(c.text)} for c in outcome.chunks
            ]
        except AppError as exc:
            context["error"] = exc.message_for(ctx.user.role == Role.ADMIN)
            status_code = exc.status_code
    return render(request, "search.html", context, status_code=status_code)
