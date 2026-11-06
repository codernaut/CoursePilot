"""Authentication: server-side sessions, login, logout, password change and login throttling.

The ``AuthProvider`` protocol is the seam for adding SSO later; ``PasswordAuthProvider`` is the
local email + argon2 implementation. Nothing here imports FastAPI.
"""

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError, validation_error
from app.db.base import utcnow
from app.models import User, UserSession
from app.services import audit
from app.services.password_policy import validate_password
from app.services.security import (
    hash_password,
    hash_token,
    new_token,
    verify_against_dummy,
    verify_password,
)

TOUCH_INTERVAL = timedelta(minutes=1)
INVALID_CREDENTIALS_MESSAGE = "Invalid email or password."
_SWEEP_THRESHOLD = 5000


def normalize_email(email: str) -> str:
    return email.strip().lower()


class AuthProvider(Protocol):
    async def authenticate(self, db: AsyncSession, email: str, password: str) -> User | None:
        """Return the active user for valid credentials, otherwise None (never say why)."""


class PasswordAuthProvider:
    async def authenticate(self, db: AsyncSession, email: str, password: str) -> User | None:
        user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None:
            verify_against_dummy(password)
            return None
        ok = verify_password(password, user.password_hash)
        if not ok or not user.is_active:
            return None
        return user


class LoginThrottle:
    """Sliding-window limiter counted per client IP and per email (in memory, per process)."""

    def __init__(
        self,
        limit_per_min: int,
        *,
        window_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit_per_min
        self.window = window_s
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def _prune(self, key: str, now: float) -> deque[float]:
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        if not hits:
            self._hits.pop(key, None)
            return deque()
        return hits

    def allow(self, ip: str, email: str) -> bool:
        """Record an attempt and return True, or return False (without recording) when limited."""
        now = self._clock()
        if len(self._hits) > _SWEEP_THRESHOLD:
            for stale in list(self._hits):
                self._prune(stale, now)
        keys = (f"ip:{ip}", f"email:{email}")
        if any(len(self._prune(key, now)) >= self.limit for key in keys):
            return False
        for key in keys:
            self._hits.setdefault(key, deque()).append(now)
        return True

    # Generic per-key variant (used for change-password attempts): the caller checks first,
    # records only failures, and clears the key after a success.
    def is_blocked(self, key: str) -> bool:
        return len(self._prune(key, self._clock())) >= self.limit

    def record(self, key: str) -> None:
        now = self._clock()
        self._prune(key, now)
        self._hits.setdefault(key, deque()).append(now)

    def clear(self, key: str) -> None:
        self._hits.pop(key, None)


@dataclass(frozen=True)
class LoginResult:
    token: str
    session: UserSession
    user: User


@dataclass(frozen=True)
class SessionContext:
    session: UserSession
    user: User


async def login(
    db: AsyncSession,
    settings: Settings,
    throttle: LoginThrottle,
    *,
    email: str,
    password: str,
    ip: str,
    user_agent: str | None,
    old_token: str | None = None,
    provider: AuthProvider | None = None,
) -> LoginResult:
    provider = provider or PasswordAuthProvider()
    email = normalize_email(email)
    if not throttle.allow(ip, email):
        raise AppError(
            "rate_limited",
            "Too many login attempts. Please wait a minute and try again.",
            status_code=429,
        )
    user = await provider.authenticate(db, email, password)
    if user is None:
        audit.record(db, "login_failed", actor_id=None, details={"email": email[:254], "ip": ip})
        await db.commit()
        raise AppError("unauthorized", INVALID_CREDENTIALS_MESSAGE, status_code=401)

    if old_token:  # session rotation: the id presented before login never survives it
        await db.execute(delete(UserSession).where(UserSession.token_hash == hash_token(old_token)))
    token = new_token()
    now = utcnow()
    session = UserSession(
        user_id=user.id,
        token_hash=hash_token(token),
        csrf_token=new_token(),
        expires_at=now + timedelta(hours=settings.session_ttl_hours),
        last_seen_at=now,
        ip=ip[:64],
        user_agent=(user_agent or "")[:300] or None,
    )
    db.add(session)
    audit.record(db, "login_succeeded", actor_id=user.id, target_type="user", target_id=user.id)
    await db.commit()
    return LoginResult(token=token, session=session, user=user)


async def resolve_session(db: AsyncSession, token: str | None) -> SessionContext | None:
    """The live session and its active user for a cookie token, or None."""
    if not token:
        return None
    row = (
        await db.execute(
            select(UserSession, User)
            .join(User, User.id == UserSession.user_id)
            .where(UserSession.token_hash == hash_token(token))
        )
    ).first()
    if row is None:
        return None
    session, user = row
    if session.expires_at <= utcnow() or not user.is_active:
        return None
    return SessionContext(session=session, user=user)


async def touch_session(db: AsyncSession, session_id: str, last_seen_at: datetime) -> None:
    """Update last_seen_at at most once a minute (call with a write session)."""
    now = utcnow()
    if now - last_seen_at < TOUCH_INTERVAL:
        return
    row = await db.get(UserSession, session_id)
    if row is not None:
        row.last_seen_at = now
        await db.commit()


async def logout(db: AsyncSession, ctx: SessionContext) -> None:
    await db.execute(delete(UserSession).where(UserSession.id == ctx.session.id))
    audit.record(db, "logout", actor_id=ctx.user.id, target_type="user", target_id=ctx.user.id)
    await db.commit()


async def revoke_user_sessions(
    db: AsyncSession, user_id: str, *, except_session_id: str | None = None
) -> None:
    stmt = delete(UserSession).where(UserSession.user_id == user_id)
    if except_session_id is not None:
        stmt = stmt.where(UserSession.id != except_session_id)
    await db.execute(stmt)


async def change_password(
    db: AsyncSession,
    ctx: SessionContext,
    throttle: LoginThrottle,
    *,
    current_password: str,
    new_password: str,
) -> None:
    user = ctx.user
    key = f"pwchange:{user.id}"
    if throttle.is_blocked(key):
        raise AppError(
            "rate_limited",
            "Too many incorrect attempts. Please wait a minute and try again.",
            status_code=429,
        )
    if not verify_password(current_password, user.password_hash):
        throttle.record(key)
        raise AppError("invalid_password", "Current password is incorrect.", status_code=400)
    throttle.clear(key)
    validate_password(new_password, email=user.email)
    if new_password == current_password:
        raise validation_error("The new password must differ from the current one.")
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    await revoke_user_sessions(db, user.id, except_session_id=ctx.session.id)
    audit.record(db, "password_changed", actor_id=user.id, target_type="user", target_id=user.id)
    await db.commit()


async def purge_expired_sessions(db: AsyncSession) -> int:
    result = await db.execute(delete(UserSession).where(UserSession.expires_at <= utcnow()))
    await db.commit()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
