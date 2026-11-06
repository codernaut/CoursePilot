from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/health", tags=["health"])


class HealthStatus(BaseModel):
    status: str


@router.get("/live", response_model=HealthStatus)
async def live() -> HealthStatus:
    """Liveness probe: the process is up. Does not touch the DB or storage."""
    return HealthStatus(status="ok")
