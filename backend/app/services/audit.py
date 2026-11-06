from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.models import AuditLog


def record(
    db: AsyncSession,
    action: str,
    *,
    actor_id: str | None,
    target_type: str | None = None,
    target_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Add an audit row to the current transaction (the caller commits)."""
    db.add(
        AuditLog(
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=details or {},
        )
    )


async def purge_old(
    db: AsyncSession,
    *,
    retention_days: int,
    login_failed_retention_days: int,
    now: datetime | None = None,
) -> int:
    """Delete audit rows older than ``retention_days``; ``login_failed`` rows (which anyone can
    generate) are kept for the shorter ``login_failed_retention_days``. Returns rows deleted."""
    now = now or utcnow()
    cutoff = now - timedelta(days=retention_days)
    failed_cutoff = now - timedelta(days=login_failed_retention_days)
    result = await db.execute(
        delete(AuditLog).where(
            or_(
                AuditLog.created_at < cutoff,
                and_(AuditLog.action == "login_failed", AuditLog.created_at < failed_cutoff),
            )
        )
    )
    await db.commit()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
