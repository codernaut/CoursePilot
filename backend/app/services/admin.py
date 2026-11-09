"""Administrator views: the job queue, usage numbers and recent answer feedback.

Privacy (CLAUDE.md section 13): conversations are private to their owner. The one exception is the
feedback view, which shows administrators an answer a user rated, the question it answered and the
comment, without the user's identity and without the rest of the conversation. The usage view shows
per-user message counts by email (administrators manage the accounts anyway) but no content.

Nothing here imports FastAPI.
"""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import conflict, not_found
from app.core.logging import redact_text
from app.db.base import utcnow
from app.models import Conversation, Document, Job, Message, User
from app.models.enums import DocumentStatus, Feedback, JobStatus, JobType, MessageRole
from app.schemas.admin import (
    DayMessages,
    DayTokens,
    FeedbackItem,
    JobOut,
    JobsPage,
    UsageOut,
    UserUsage,
)
from app.services import audit, chat, documents

MAX_PAGE_SIZE = 200
FEEDBACK_ANSWER_CHARS = 2000
FEEDBACK_QUESTION_CHARS = 1000
TOP_USERS = 20


# -- jobs ------------------------------------------------------------------------------------


async def list_jobs(
    db: AsyncSession,
    *,
    status: JobStatus | None = None,
    job_type: JobType | None = None,
    job_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JobsPage:
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    offset = max(0, offset)
    conditions = []
    if status is not None:
        conditions.append(Job.status == status)
    if job_type is not None:
        conditions.append(Job.type == job_type)
    if job_id is not None:
        conditions.append(Job.id == job_id)
    total = int(
        (await db.execute(select(func.count()).select_from(Job).where(*conditions))).scalar_one()
    )
    jobs = list(
        (
            await db.execute(
                select(Job)
                .where(*conditions)
                .order_by(Job.created_at.desc(), Job.id)
                .limit(limit)
                .offset(offset)
            )
        ).scalars()
    )
    ids = {j.payload.get("document_id") for j in jobs} - {None}
    titles: dict[str, str] = {}
    if ids:
        rows = await db.execute(select(Document.id, Document.title).where(Document.id.in_(ids)))
        titles = {doc_id: title for doc_id, title in rows}
    items = []
    for job in jobs:
        raw = job.payload.get("document_id")
        document_id = raw if isinstance(raw, str) else None
        items.append(
            JobOut(
                id=job.id,
                type=job.type,
                status=job.status,
                attempts=job.attempts,
                max_attempts=job.max_attempts,
                run_after=job.run_after,
                locked_at=job.locked_at,
                locked_by=job.locked_by,
                last_error=redact_text(job.last_error) if job.last_error else None,
                document_id=document_id,
                document_title=titles.get(document_id) if document_id else None,
                created_at=job.created_at,
                updated_at=job.updated_at,
            )
        )
    return JobsPage(items=items, total=total, limit=limit, offset=offset)


async def retry_job(db: AsyncSession, actor: User, job_id: str) -> Job:
    """Re-queue a ``failed`` job (attempts reset, error cleared). Audited."""
    job = await db.get(Job, job_id)
    if job is None:
        raise not_found("Job not found.")
    if job.status != JobStatus.FAILED:
        raise conflict("Only failed jobs can be retried.")
    job.status = JobStatus.QUEUED
    job.attempts = 0
    job.run_after = utcnow()
    job.locked_at = None
    job.locked_by = None
    job.last_error = None
    if job.type != JobType.DELETE_DOCUMENT:
        await documents.mark_job_requeued(db, job.payload.get("document_id"))
    audit.record(
        db,
        "job_retried",
        actor_id=actor.id,
        target_type="job",
        target_id=job.id,
        details={"type": job.type.value, "document_id": job.payload.get("document_id")},
    )
    await db.commit()
    return job


# -- usage -----------------------------------------------------------------------------------


def provider_of(model: str | None) -> str:
    """The messages table stores the model only; the provider is inferred from its name."""
    name = (model or "").lower()
    if name.startswith("claude"):
        return "anthropic"
    if name.startswith("gemini"):
        return "gemini"
    if name.startswith("fake"):
        return "fake"
    return "unknown"


def _today() -> datetime:
    return utcnow().replace(hour=0, minute=0, second=0, microsecond=0)


def _tokens(key: str) -> Any:
    return func.coalesce(func.json_extract(Message.token_usage, f"$.{key}"), 0)


async def usage(db: AsyncSession, settings: Settings, *, days: int = 30) -> UsageOut:
    """Numbers for the last ``days`` UTC days (today included)."""
    days = max(1, min(days, 365))
    today = _today()
    start = today - timedelta(days=days - 1)
    day = func.date(Message.created_at)

    per_day = (
        await db.execute(
            select(
                day,
                func.sum(case((Message.role == MessageRole.USER, 1), else_=0)),
                func.sum(case((Message.role == MessageRole.ASSISTANT, 1), else_=0)),
            )
            .where(Message.created_at >= start)
            .group_by(day)
            .order_by(day)
        )
    ).all()

    per_model = (
        await db.execute(
            select(
                day,
                Message.model,
                func.sum(_tokens("input_tokens")),
                func.sum(_tokens("output_tokens")),
                func.sum(_tokens("total_tokens")),
                func.sum(_tokens("cache_read_input_tokens")),
                func.sum(_tokens("cache_creation_input_tokens")),
            )
            .where(Message.created_at >= start, Message.role == MessageRole.ASSISTANT)
            .group_by(day, Message.model)
            .order_by(day, Message.model)
        )
    ).all()

    users = (
        await db.execute(
            select(
                User.email,
                User.display_name,
                User.role,
                func.sum(case((Message.created_at >= today, 1), else_=0)),
                func.count(),
            )
            .select_from(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .join(User, User.id == Conversation.user_id)
            .where(Message.role == MessageRole.USER, Message.created_at >= start)
            .group_by(User.id)
            .order_by(func.count().desc(), User.email)
            .limit(TOP_USERS)
        )
    ).all()

    answers = (
        await db.execute(
            select(
                func.count(),
                func.sum(case((Message.used_web.is_(True), 1), else_=0)),
            ).where(Message.role == MessageRole.ASSISTANT, Message.created_at >= start)
        )
    ).one()

    doc_status = (
        await db.execute(select(Document.status, func.count()).group_by(Document.status))
    ).all()
    storage = (
        await db.execute(
            select(func.coalesce(func.sum(Document.size_bytes), 0)).where(
                Document.status != DocumentStatus.DELETING
            )
        )
    ).scalar_one()
    job_status = (await db.execute(select(Job.status, func.count()).group_by(Job.status))).all()

    return UsageOut(
        days=days,
        messages_per_day=[
            DayMessages(day=d, questions=int(q or 0), answers=int(a or 0)) for d, q, a in per_day
        ],
        tokens_per_day=[
            DayTokens(
                day=d,
                provider=provider_of(model),
                model=model or "unknown",
                input_tokens=int(i or 0),
                output_tokens=int(o or 0),
                total_tokens=int(t or 0),
                cache_read_tokens=int(cr or 0),
                cache_creation_tokens=int(cc or 0),
            )
            for d, model, i, o, t, cr, cc in per_model
        ],
        top_users=[
            UserUsage(
                email=email,
                display_name=name,
                role=role.value,
                messages_today=int(n_today or 0),
                messages_in_period=int(n_period),
                daily_limit=settings.chat_daily_limit_per_user,
            )
            for email, name, role, n_today, n_period in users
        ],
        documents_by_status={s.value: int(n) for s, n in doc_status},
        jobs_by_status={s.value: int(n) for s, n in job_status},
        storage_bytes=int(storage),
        web_answers=int(answers[1] or 0),
        total_answers=int(answers[0] or 0),
    )


# -- feedback --------------------------------------------------------------------------------


async def recent_feedback(
    db: AsyncSession, *, rating: Feedback | None = Feedback.DOWN, limit: int = 50
) -> list[FeedbackItem]:
    """Recently rated answers (thumbs down by default), newest first: the rated answer, the
    question it answered and the comment. No user identity, nothing else of the conversation."""
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    condition = Message.feedback == rating if rating else Message.feedback.is_not(None)
    rows = list(
        (
            await db.execute(
                select(Message, Conversation.module_id)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(Message.role == MessageRole.ASSISTANT, condition)
                .order_by(Message.updated_at.desc(), Message.id)
                .limit(limit)
            )
        ).all()
    )
    paths = await chat.module_paths(db, {m for _, m in rows if m})
    items: list[FeedbackItem] = []
    for message, module_id in rows:
        question = (
            await db.execute(
                select(Message.content)
                .where(
                    Message.conversation_id == message.conversation_id,
                    Message.role == MessageRole.USER,
                    Message.created_at <= message.created_at,
                )
                .order_by(Message.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        items.append(
            FeedbackItem(
                message_id=message.id,
                rating="up" if message.feedback == Feedback.UP else "down",
                comment=message.feedback_comment,
                module_path=paths.get(module_id or ""),
                question=question[:FEEDBACK_QUESTION_CHARS] if question else None,
                answer=message.content[:FEEDBACK_ANSWER_CHARS],
                created_at=message.created_at,
            )
        )
    return items
