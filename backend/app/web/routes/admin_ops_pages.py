"""Administrator pages: background jobs (with retry) and usage / feedback."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from starlette.responses import RedirectResponse, Response

from app.api.deps import DbDep, SettingsDep, require_role
from app.core.errors import AppError
from app.models.enums import Feedback, JobStatus, JobType, Role
from app.services import admin
from app.services.auth import SessionContext
from app.web.common import render

router = APIRouter(prefix="/admin", include_in_schema=False)

AdminDep = Depends(require_role(Role.ADMIN))
PAGE_SIZE = 25


def _enum_or_none[E: (JobStatus, JobType)](cls: type[E], value: str) -> E | None:
    try:
        return cls(value) if value else None
    except ValueError:
        return None


async def _jobs_context(db: DbDep, status: str, type_: str, offset: int) -> dict[str, object]:
    status_value = _enum_or_none(JobStatus, status)
    type_value = _enum_or_none(JobType, type_)
    page = await admin.list_jobs(
        db, status=status_value, job_type=type_value, limit=PAGE_SIZE, offset=offset
    )
    return {
        "page": page,
        "status_filter": status_value.value if status_value else "",
        "type_filter": type_value.value if type_value else "",
        "statuses": [s.value for s in JobStatus],
        "types": [t.value for t in JobType],
    }


@router.get("/jobs")
async def jobs_page(
    request: Request,
    db: DbDep,
    _: SessionContext = AdminDep,
    status: Annotated[str, Query(max_length=20)] = "",
    type: Annotated[str, Query(max_length=30)] = "",
    offset: Annotated[int, Query(ge=0)] = 0,
    notice: Annotated[str, Query(max_length=20)] = "",
) -> Response:
    context = await _jobs_context(db, status, type, offset)
    context["notice"] = "The job was queued again." if notice == "retried" else None
    return render(request, "admin_jobs.html", context)


@router.get("/jobs/table")
async def jobs_table(
    request: Request,
    db: DbDep,
    _: SessionContext = AdminDep,
    status: Annotated[str, Query(max_length=20)] = "",
    type: Annotated[str, Query(max_length=30)] = "",
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Response:
    """The table on its own: swapped in by HTMX every few seconds."""
    return render(request, "_jobs_table.html", await _jobs_context(db, status, type, offset))


@router.post("/jobs/{job_id}/retry")
async def retry_job(
    request: Request, job_id: str, db: DbDep, ctx: SessionContext = AdminDep
) -> Response:
    try:
        await admin.retry_job(db, ctx.user, job_id)
    except AppError as exc:
        if exc.status_code == 404:
            raise
        context = await _jobs_context(db, "", "", 0)
        context["error"] = exc.message
        return render(request, "admin_jobs.html", context, status_code=exc.status_code)
    return RedirectResponse("/admin/jobs?notice=retried", status_code=303)


@router.get("/usage")
async def usage_page(
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    _: SessionContext = AdminDep,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> Response:
    return render(
        request,
        "admin_usage.html",
        {
            "usage": await admin.usage(db, settings, days=days),
            "feedback": await admin.recent_feedback(db, rating=Feedback.DOWN, limit=25),
            "daily_limit": settings.chat_daily_limit_per_user,
        },
    )
