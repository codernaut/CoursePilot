"""Administrator JSON API: job queue, usage numbers and recent answer feedback (admin only)."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from app.api.deps import DbDep, SettingsDep, require_role
from app.models.enums import Feedback, JobStatus, JobType, Role
from app.schemas.admin import FeedbackItem, JobOut, JobsPage, UsageOut
from app.services import admin
from app.services.auth import SessionContext

router = APIRouter(prefix="/admin", tags=["admin"])

AdminDep = Depends(require_role(Role.ADMIN))


@router.get("/jobs", response_model=JobsPage)
async def list_jobs(
    db: DbDep,
    _: SessionContext = AdminDep,
    status: JobStatus | None = None,
    type: Annotated[JobType | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=admin.MAX_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JobsPage:
    return await admin.list_jobs(db, status=status, job_type=type, limit=limit, offset=offset)


@router.post("/jobs/{job_id}/retry", response_model=JobOut)
async def retry_job(job_id: str, db: DbDep, ctx: SessionContext = AdminDep) -> JobOut:
    """Re-queue a failed job. Anything else is 409."""
    job = await admin.retry_job(db, ctx.user, job_id)
    return (await admin.list_jobs(db, job_id=job.id, limit=1)).items[0]


@router.get("/usage", response_model=UsageOut)
async def usage(
    db: DbDep,
    settings: SettingsDep,
    _: SessionContext = AdminDep,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> UsageOut:
    return await admin.usage(db, settings, days=days)


@router.get("/feedback", response_model=list[FeedbackItem])
async def feedback(
    db: DbDep,
    _: SessionContext = AdminDep,
    rating: Annotated[Literal["up", "down", "all"], Query()] = "down",
    limit: Annotated[int, Query(ge=1, le=admin.MAX_PAGE_SIZE)] = 50,
) -> list[FeedbackItem]:
    """Recently rated answers with the question and comment, without the user's identity."""
    return await admin.recent_feedback(
        db, rating=None if rating == "all" else Feedback(rating), limit=limit
    )
