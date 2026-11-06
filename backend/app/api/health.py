from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from app import __version__
from app.api.deps import DatabaseDep
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.version import get_commit

router = APIRouter(prefix="/health", tags=["health"])
log = get_logger(__name__)


class HealthStatus(BaseModel):
    status: str


class ReadyStatus(BaseModel):
    status: str
    checks: dict[str, str]


class VersionInfo(BaseModel):
    version: str
    commit: str


@router.get("/live", response_model=HealthStatus)
async def live() -> HealthStatus:
    """Liveness probe: the process is up. Does not touch the DB or storage."""
    return HealthStatus(status="ok")


@router.get("/ready", response_model=ReadyStatus)
async def ready(database: DatabaseDep) -> ReadyStatus:
    """Readiness probe: the database answers and sqlite-vec is loaded."""
    try:
        async with database.read_session() as session:
            await session.execute(text("SELECT 1"))
            await session.execute(text("SELECT vec_version()"))
    except Exception as exc:
        log.warning("not_ready", error=type(exc).__name__)
        raise AppError(
            "not_ready",
            "Database or sqlite-vec is not available.",
            status_code=503,
            details={"checks": {"database": "error"}},
        ) from exc
    return ReadyStatus(status="ok", checks={"database": "ok", "sqlite_vec": "ok"})


@router.get("/version", response_model=VersionInfo)
async def version() -> VersionInfo:
    """Public: the running version and git commit (no secrets, useful in bug reports)."""
    return VersionInfo(version=__version__, commit=get_commit())
