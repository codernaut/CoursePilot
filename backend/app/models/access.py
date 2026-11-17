"""Temporary module access (M9): codes that admit guests, and the guest accounts they created."""

from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, BaseModel, UTCDateTime


class AccessCode(BaseModel, Base):
    """A per-module code. Only the sha256 of the normalised code is stored; the plain code is
    shown once, at creation. ``expires_at`` is fixed at creation (creation time + duration)."""

    __tablename__ = "access_codes"
    __table_args__ = (Index("ix_access_codes_module_id", "module_id"),)

    module_id: Mapped[str] = mapped_column(ForeignKey("modules.id"), nullable=False)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    label: Mapped[str | None] = mapped_column(String(80))
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    code_hint: Mapped[str] = mapped_column(String(4), nullable=False)
    duration_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    redemptions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class GuestAccount(BaseModel, Base):
    """Links a ``users`` row with role ``guest`` to the code it was admitted with."""

    __tablename__ = "guest_accounts"
    __table_args__ = (Index("ix_guest_accounts_access_code_id", "access_code_id"),)

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), unique=True, nullable=False)
    access_code_id: Mapped[str] = mapped_column(ForeignKey("access_codes.id"), nullable=False)
