from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from app import __version__
from app.api.deps import DatabaseDep, StorageDep
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
async def ready(database: DatabaseDep, storage: StorageDep) -> ReadyStatus:
    """Readiness probe: the database answers, sqlite-vec is loaded and storage is writable."""
    checks = {"database": "ok", "sqlite_vec": "ok", "storage": "ok"}
    try:
        async with database.read_session() as session:
            await session.execute(text("SELECT 1"))
            await session.execute(text("SELECT vec_version()"))
    except Exception as exc:
        log.warning("not_ready", error=type(exc).__name__)
        checks["database"] = checks["sqlite_vec"] = "error"
    try:
        await storage.check()
    except Exception as exc:
        log.warning("storage_not_ready", error=type(exc).__name__)
        checks["storage"] = "error"
    if "error" in checks.values():
        raise AppError(
            "not_ready",
            "Database, sqlite-vec or storage is not available.",
            status_code=503,
            details={"checks": checks},
        )
    return ReadyStatus(status="ok", checks=checks)


@router.get("/version", response_model=VersionInfo)
async def version() -> VersionInfo:
    """Public: the running version and git commit (no secrets, useful in bug reports)."""
    return VersionInfo(version=__version__, commit=get_commit())
