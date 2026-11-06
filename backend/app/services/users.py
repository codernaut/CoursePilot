"""User management. Admin-only operations check the actor's role here, not only in routers."""

import re

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import conflict, forbidden, not_found, validation_error
from app.models import User
from app.models.enums import Role
from app.services import audit
from app.services.auth import normalize_email, revoke_user_sessions
from app.services.password_policy import validate_password
from app.services.security import generate_temporary_password, hash_password

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MAX_NAME = 120


def _require_admin(actor: User) -> None:
    if actor.role != Role.ADMIN or not actor.is_active:
        raise forbidden("Only administrators can manage users.")


def _clean_name(name: str) -> str:
    cleaned = " ".join(name.split())
    if not cleaned or len(cleaned) > _MAX_NAME:
        raise validation_error(f"Display name must be 1-{_MAX_NAME} characters.")
    return cleaned


def _clean_email(email: str) -> str:
    cleaned = normalize_email(email)
    if len(cleaned) > 254 or not _EMAIL_RE.match(cleaned):
        raise validation_error("Enter a valid email address.")
    return cleaned


async def get_user(db: AsyncSession, actor: User, user_id: str) -> User:
    _require_admin(actor)
    return await _load(db, user_id)


async def _load(db: AsyncSession, user_id: str) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise not_found("User not found.")
    return user


async def list_users(db: AsyncSession, actor: User) -> list[User]:
    _require_admin(actor)
    return list((await db.execute(select(User).order_by(User.email))).scalars())


async def count_active_admins(db: AsyncSession) -> int:
    stmt = (
        select(func.count())
        .select_from(User)
        .where(User.role == Role.ADMIN, User.is_active.is_(True))
    )
    return int((await db.execute(stmt)).scalar_one())


async def admin_exists(db: AsyncSession) -> bool:
    return await count_active_admins(db) > 0


async def _insert_user(
    db: AsyncSession,
    *,
    email: str,
    display_name: str,
    role: Role,
    password: str,
    must_change_password: bool,
) -> User:
    email = _clean_email(email)
    exists = (await db.execute(select(User.id).where(User.email == email))).first()
    if exists:
        raise conflict("A user with that email already exists.")
    user = User(
        email=email,
        display_name=_clean_name(display_name),
        role=role,
        password_hash=hash_password(password),
        is_active=True,
        must_change_password=must_change_password,
    )
    db.add(user)
    await db.flush()
    return user


async def create_initial_admin(
    db: AsyncSession, *, email: str, display_name: str, password: str
) -> User:
    """Bootstrap path used by the CLI (no acting user yet). The admin chose the password."""
    validate_password(password, email=email)
    user = await _insert_user(
        db,
        email=email,
        display_name=display_name,
        role=Role.ADMIN,
        password=password,
        must_change_password=False,
    )
    audit.record(
        db,
        "user_created",
        actor_id=None,
        target_type="user",
        target_id=user.id,
        details={"role": Role.ADMIN.value, "via": "cli"},
    )
    await db.commit()
    return user


async def create_user(
    db: AsyncSession,
    actor: User,
    *,
    email: str,
    display_name: str,
    role: Role,
    password: str | None = None,
) -> tuple[User, str | None]:
    """Create an account. Returns the user and the generated temporary password (if the admin
    did not supply one). The user must change the password at first login."""
    _require_admin(actor)
    generated: str | None = None
    if not password:
        generated = generate_temporary_password()
        password = generated
    else:
        validate_password(password, email=email)
    user = await _insert_user(
        db,
        email=email,
        display_name=display_name,
        role=role,
        password=password,
        must_change_password=True,
    )
    audit.record(
        db,
        "user_created",
        actor_id=actor.id,
        target_type="user",
        target_id=user.id,
        details={"role": role.value, "email": user.email},
    )
    await db.commit()
    return user, generated


async def update_user(
    db: AsyncSession,
    actor: User,
    user_id: str,
    *,
    display_name: str | None = None,
    role: Role | None = None,
    is_active: bool | None = None,
) -> User:
    _require_admin(actor)
    user = await _load(db, user_id)
    losing_admin = user.role == Role.ADMIN and user.is_active
    demoting = role is not None and role != user.role and user.role == Role.ADMIN
    deactivating = is_active is False and user.is_active

    if user.id == actor.id and (demoting or deactivating):
        raise conflict("You cannot demote or deactivate your own account.")
    if losing_admin and (demoting or deactivating) and await count_active_admins(db) <= 1:
        raise conflict("The last active administrator cannot be removed.")

    if display_name is not None:
        user.display_name = _clean_name(display_name)
    if role is not None and role != user.role:
        audit.record(
            db,
            "user_role_changed",
            actor_id=actor.id,
            target_type="user",
            target_id=user.id,
            details={"from": user.role.value, "to": role.value},
        )
        user.role = role
    if is_active is not None and is_active != user.is_active:
        user.is_active = is_active
        audit.record(
            db,
            "user_reactivated" if is_active else "user_deactivated",
            actor_id=actor.id,
            target_type="user",
            target_id=user.id,
        )
        if not is_active:
            await revoke_user_sessions(db, user.id)
    await db.commit()
    return user


async def reset_password(db: AsyncSession, actor: User, user_id: str) -> tuple[User, str]:
    """Generate a one-time temporary password. It is returned to the caller once and never
    logged or stored (only its hash)."""
    _require_admin(actor)
    user = await _load(db, user_id)
    temporary = generate_temporary_password()
    user.password_hash = hash_password(temporary)
    user.must_change_password = True
    await revoke_user_sessions(db, user.id)
    audit.record(db, "password_reset", actor_id=actor.id, target_type="user", target_id=user.id)
    await db.commit()
    return user, temporary
