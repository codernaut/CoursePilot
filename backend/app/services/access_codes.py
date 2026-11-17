"""Temporary module access: codes created by faculty/admins and the guests they admit (M9).

A code belongs to one module and is valid from its creation until ``expires_at`` (creation time plus
the chosen duration) or until it is revoked. Guests have no account and no password: redeeming a
code creates a ``users`` row with role ``guest`` (synthetic, non-routable email, unusable password
hash), a ``guest_accounts`` row linking it to the code, and a normal server-side session that ends
no later than the code does. Keeping guests in ``users`` means conversations, messages, quotas,
rate limits and the access filter work for them exactly as for accounts; ``list_users`` hides them.

Nothing here imports FastAPI.
"""

import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError, conflict, forbidden, not_found, validation_error
from app.db.base import new_id, utcnow
from app.models import (
    AccessCode,
    AuditLog,
    Conversation,
    GuestAccount,
    Message,
    Module,
    User,
    UserSession,
)
from app.models.enums import Role
from app.services import audit
from app.services.auth import LoginResult, LoginThrottle
from app.services.security import hash_token, new_token

ALLOWED_DURATIONS: tuple[int, ...] = (2, 4, 8, 10, 12, 24)
# Unambiguous alphabet: no 0/O, 1/I/L. 31 symbols ** 11 characters is about 54 bits.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 11
CODE_GROUPS = (4, 4, 3)
MAX_LABEL = 80
MAX_GUEST_NAME = 40
RECENT_WINDOW = timedelta(days=3)  # expired/revoked codes stay listed this long
GUEST_DATA_GRACE = timedelta(hours=1)  # guest chats are deleted this long after access ended
GUEST_EMAIL_DOMAIN = "guest.invalid"  # reserved TLD: never routable
UNUSABLE_PASSWORD_HASH = "!"  # noqa: S105 - not a hash: verification always fails
INVALID_CODE_MESSAGE = "That access code is not valid or has expired."
ACCESS_ENDED_MESSAGE = "Your temporary access has ended."

_NON_ALNUM = re.compile(r"[^A-Z0-9]")


def generate_code() -> str:
    """A random code like ``ABCD-EFGH-JKM`` (54 bits of entropy), from ``secrets``."""
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    parts, start = [], 0
    for size in CODE_GROUPS:
        parts.append(raw[start : start + size])
        start += size
    return "-".join(parts)


def normalize_code(code: str) -> str:
    """Case-insensitive, ignoring spaces, dashes and any other separators."""
    return _NON_ALNUM.sub("", code.upper())


def hash_code(code: str) -> str:
    return hash_token(normalize_code(code))


def _clean_text(value: str | None, limit: int) -> str:
    return " ".join("".join(c if c.isprintable() else " " for c in (value or "")).split())[:limit]


def clean_label(label: str | None) -> str | None:
    cleaned = _clean_text(label, MAX_LABEL)
    return cleaned or None


def clean_guest_name(name: str) -> str:
    cleaned = _clean_text(name, 10_000)
    if not cleaned or len(cleaned) > MAX_GUEST_NAME:
        raise validation_error(f"Enter your name (1-{MAX_GUEST_NAME} characters).")
    return cleaned


def _require_issuer(actor: User) -> None:
    if actor.role not in (Role.ADMIN, Role.FACULTY) or not actor.is_active:
        raise forbidden("Only faculty and administrators manage access codes.")


def is_valid(code: AccessCode, now: datetime | None = None) -> bool:
    return code.revoked_at is None and code.expires_at > (now or utcnow())


def status_of(code: AccessCode, now: datetime | None = None) -> str:
    """``active``, ``revoked`` or ``expired`` (revoked wins)."""
    if code.revoked_at is not None:
        return "revoked"
    return "active" if code.expires_at > (now or utcnow()) else "expired"


# -- faculty / admin side ---------------------------------------------------------------------


@dataclass(frozen=True)
class CodeView:
    code: AccessCode
    status: str
    created_by_name: str


async def create_code(
    db: AsyncSession,
    actor: User,
    module_id: str,
    *,
    duration_hours: int,
    label: str | None = None,
) -> tuple[AccessCode, str]:
    """Create a code that is valid from now for ``duration_hours``. Returns it with the plain code,
    which exists nowhere else afterwards."""
    _require_issuer(actor)
    if duration_hours not in ALLOWED_DURATIONS:
        raise validation_error(
            "Choose a duration of " + ", ".join(str(h) for h in ALLOWED_DURATIONS) + " hours.",
            {"allowed": list(ALLOWED_DURATIONS)},
        )
    module = await db.get(Module, module_id)
    if module is None:
        raise not_found("Module not found.")
    plain = generate_code()
    now = utcnow()
    code = AccessCode(
        module_id=module.id,
        created_by=actor.id,
        label=clean_label(label),
        code_hash=hash_code(plain),
        code_hint=normalize_code(plain)[-4:],
        duration_hours=duration_hours,
        expires_at=now + timedelta(hours=duration_hours),
        redemptions=0,
        created_at=now,  # the period starts here: expires_at is exactly created_at + duration
    )
    db.add(code)
    await db.flush()
    audit.record(
        db,
        "access_code_created",
        actor_id=actor.id,
        target_type="access_code",
        target_id=code.id,
        details={"module_id": module.id, "duration_hours": duration_hours},
    )
    await db.commit()
    return code, plain


async def list_codes(
    db: AsyncSession, actor: User, module_id: str, *, now: datetime | None = None
) -> list[CodeView]:
    """Active codes and those that ended within the last few days, newest first. Never the plain
    codes (they are not stored)."""
    _require_issuer(actor)
    if await db.get(Module, module_id) is None:
        raise not_found("Module not found.")
    now = now or utcnow()
    rows = (
        await db.execute(
            select(AccessCode, User.display_name)
            .join(User, User.id == AccessCode.created_by)
            .where(
                AccessCode.module_id == module_id,
                AccessCode.expires_at > now - RECENT_WINDOW,
            )
            .order_by(AccessCode.created_at.desc(), AccessCode.id)
        )
    ).all()
    return [CodeView(code, status_of(code, now), name) for code, name in rows]


async def revoke_code(db: AsyncSession, actor: User, code_id: str) -> AccessCode:
    """End a code now. Guest sessions stop working on their very next request (the code is
    re-checked per request); the rows themselves are removed by the worker's housekeeping."""
    _require_issuer(actor)
    code = await db.get(AccessCode, code_id)
    if code is None:
        raise not_found("Access code not found.")
    if status_of(code) != "active":
        raise conflict("This access code has already ended.")
    code.revoked_at = utcnow()
    code.revoked_by = actor.id
    audit.record(
        db,
        "access_code_revoked",
        actor_id=actor.id,
        target_type="access_code",
        target_id=code.id,
        details={"module_id": code.module_id},
    )
    await db.commit()
    return code


async def delete_guest_sessions(db: AsyncSession, code_ids: list[str]) -> int:
    if not code_ids:
        return 0
    guest_ids = select(GuestAccount.user_id).where(GuestAccount.access_code_id.in_(code_ids))
    result = await db.execute(delete(UserSession).where(UserSession.user_id.in_(guest_ids)))
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


# -- guest side -------------------------------------------------------------------------------


async def get_guest_code(db: AsyncSession, user_id: str) -> AccessCode | None:
    """The access code a guest user was admitted with (valid or not)."""
    return (
        await db.execute(
            select(AccessCode)
            .join(GuestAccount, GuestAccount.access_code_id == AccessCode.id)
            .where(GuestAccount.user_id == user_id)
        )
    ).scalar_one_or_none()


async def redeem(
    db: AsyncSession,
    settings: Settings,
    throttle: LoginThrottle,
    *,
    code: str,
    name: str,
    ip: str,
    user_agent: str | None,
    old_token: str | None = None,
) -> LoginResult:
    """Admit a guest: a new guest user and session that ends with the code. Every failure (unknown,
    expired, revoked) is the same ``401`` so codes cannot be probed; only failed attempts count
    against the per-IP limit, so a class redeeming from one network is not locked out."""
    key = f"guest:{ip}"
    if throttle.is_blocked(key):
        raise AppError(
            "rate_limited",
            "Too many attempts. Please wait a minute and try again.",
            status_code=429,
            headers={"Retry-After": str(throttle.retry_after(key))},
        )
    display_name = clean_guest_name(name)
    normalised = normalize_code(code)
    found = None
    if 0 < len(normalised) <= 64:
        found = (
            await db.execute(
                select(AccessCode).where(AccessCode.code_hash == hash_token(normalised))
            )
        ).scalar_one_or_none()
    now = utcnow()
    if found is None or not is_valid(found, now):
        throttle.record(key)
        audit.record(
            db, "login_failed", actor_id=None, details={"via": "guest_code", "ip": ip[:64]}
        )
        await db.commit()
        raise AppError("unauthorized", INVALID_CODE_MESSAGE, status_code=401)

    if old_token:
        await db.execute(delete(UserSession).where(UserSession.token_hash == hash_token(old_token)))
    user_id = new_id()
    user = User(
        id=user_id,
        email=f"guest-{user_id}@{GUEST_EMAIL_DOMAIN}",
        password_hash=UNUSABLE_PASSWORD_HASH,
        display_name=display_name,
        role=Role.GUEST,
        is_active=True,
        must_change_password=False,
    )
    db.add(user)
    await db.flush()
    db.add(GuestAccount(user_id=user.id, access_code_id=found.id))
    token = new_token()
    session = UserSession(
        user_id=user.id,
        token_hash=hash_token(token),
        csrf_token=new_token(),
        expires_at=min(found.expires_at, now + timedelta(hours=settings.session_ttl_hours)),
        last_seen_at=now,
        ip=ip[:64],
        user_agent=(user_agent or "")[:300] or None,
    )
    db.add(session)
    found.redemptions += 1
    audit.record(
        db,
        "guest_redeemed",
        actor_id=user.id,
        target_type="access_code",
        target_id=found.id,
        details={"module_id": found.module_id},
    )
    await db.commit()
    return LoginResult(token=token, session=session, user=user)


# -- cleanup (worker housekeeping) -------------------------------------------------------------


@dataclass(frozen=True)
class GuestPurge:
    sessions: int = 0
    guests: int = 0
    thread_ids: tuple[str, ...] = ()


async def purge_ended_guests(
    db: AsyncSession, *, now: datetime | None = None, grace: timedelta = GUEST_DATA_GRACE
) -> GuestPurge:
    """For codes that expired or were revoked: delete their guest sessions at once, and, once the
    grace period has passed, the guests' conversations, messages and accounts. Audit rows are kept
    (their actor is cleared). Returns the checkpoint thread ids the caller must delete."""
    now = now or utcnow()
    ended = (
        await db.execute(
            select(AccessCode.id, AccessCode.expires_at, AccessCode.revoked_at).where(
                (AccessCode.revoked_at.is_not(None)) | (AccessCode.expires_at <= now)
            )
        )
    ).all()
    if not ended:
        return GuestPurge()
    sessions = await delete_guest_sessions(db, [row[0] for row in ended])
    old_ids = [
        code_id
        for code_id, expires_at, revoked_at in ended
        if min(expires_at, revoked_at or expires_at) <= now - grace
    ]
    guests = 0
    threads: list[str] = []
    if old_ids:
        guest_ids = list(
            (
                await db.execute(
                    select(GuestAccount.user_id).where(GuestAccount.access_code_id.in_(old_ids))
                )
            ).scalars()
        )
        if guest_ids:
            conversations = (
                await db.execute(
                    select(Conversation.id, Conversation.thread_id).where(
                        Conversation.user_id.in_(guest_ids)
                    )
                )
            ).all()
            conversation_ids = [c for c, _ in conversations]
            threads = [t for _, t in conversations]
            if conversation_ids:
                await db.execute(
                    delete(Message).where(Message.conversation_id.in_(conversation_ids))
                )
                await db.execute(delete(Conversation).where(Conversation.id.in_(conversation_ids)))
            await db.execute(delete(UserSession).where(UserSession.user_id.in_(guest_ids)))
            await db.execute(
                update(AuditLog).where(AuditLog.actor_id.in_(guest_ids)).values(actor_id=None)
            )
            await db.execute(delete(GuestAccount).where(GuestAccount.user_id.in_(guest_ids)))
            await db.execute(delete(User).where(User.id.in_(guest_ids)))
            guests = len(guest_ids)
    await db.commit()
    return GuestPurge(sessions=sessions, guests=guests, thread_ids=tuple(threads))
