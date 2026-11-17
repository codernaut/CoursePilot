from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.services.access_codes import CodeView


class AccessCodeCreate(BaseModel):
    duration_hours: int
    label: str | None = Field(default=None, max_length=200)


class AccessCodeOut(BaseModel):
    id: str
    module_id: str
    label: str | None
    code_hint: str
    duration_hours: int
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    status: Literal["active", "expired", "revoked"]
    created_by_name: str
    redemptions: int

    @classmethod
    def from_view(cls, view: CodeView) -> "AccessCodeOut":
        code = view.code
        return cls(
            id=code.id,
            module_id=code.module_id,
            label=code.label,
            code_hint=code.code_hint,
            duration_hours=code.duration_hours,
            created_at=code.created_at,
            expires_at=code.expires_at,
            revoked_at=code.revoked_at,
            status=view.status,
            created_by_name=view.created_by_name,
            redemptions=code.redemptions,
        )


class AccessCodeCreated(AccessCodeOut):
    """Returned once, at creation: the only time the plain code is available."""

    code: str
