"""Shared FastAPI dependencies: settings, DB sessions, authentication, CSRF and role checks.

Used by both the JSON API and the HTML page routes so they enforce identical rules.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError, forbidden, unauthorized
from app.db.session import Database
from app.models.enums import Role
from app.services import auth
from app.services.auth import LoginThrottle, SessionContext
from app.services.security import (
    constant_time_equals,
    origin_allowed,
    verify_login_token,
)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CSRF_HEADER = "X-CSRF-Token"
CSRF_FIELD = "csrf_token"
PASSWORD_CHANGE_REQUIRED = "password_change_required"  # noqa: S105 - an error code


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_database(request: Request) -> Database:
    database: Database = request.app.state.database
    return database


def get_throttle(request: Request) -> LoginThrottle:
    throttle: LoginThrottle = request.app.state.login_throttle
    return throttle


def get_password_throttle(request: Request) -> LoginThrottle:
    throttle: LoginThrottle = request.app.state.password_throttle
    return throttle


SettingsDep = Annotated[Settings, Depends(get_settings)]
DatabaseDep = Annotated[Database, Depends(get_database)]


async def get_db(request: Request, database: DatabaseDep) -> AsyncIterator[AsyncSession]:
    """Read-only (deferred BEGIN) session for safe methods, BEGIN IMMEDIATE otherwise."""
    session = database.read_session() if request.method in SAFE_METHODS else database.session()
    async with session:
        yield session


DbDep = Annotated[AsyncSession, Depends(get_db)]


def cookie_name(settings: Settings) -> str:
    return "__Host-session" if settings.secure_cookies else "session"


def get_session_token(request: Request, settings: Settings) -> str | None:
    return request.cookies.get(cookie_name(settings))


def set_session_cookie(response: Response, settings: Settings, token: str) -> None:
    response.set_cookie(
        cookie_name(settings),
        token,
        max_age=settings.session_ttl_hours * 3600,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookies,
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        cookie_name(settings),
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookies,
    )


def client_ip(request: Request, settings: Settings) -> str:
    """The client address. X-Forwarded-For is only trusted when BEHIND_PROXY is set, and then
    only its last entry (appended by our own proxy) is used."""
    if settings.behind_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.client.host if request.client else "unknown"


def check_origin(request: Request, settings: Settings) -> None:
    if not origin_allowed(settings, request.headers.get("origin"), request.headers.get("referer")):
        raise AppError("forbidden", "Cross-origin request rejected.", status_code=403)


async def submitted_csrf_token(request: Request) -> str | None:
    header = request.headers.get(CSRF_HEADER)
    if header:
        return header
    content_type = request.headers.get("content-type", "")
    if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        form = await request.form()
        value = form.get(CSRF_FIELD)
        if isinstance(value, str):
            return value
    return None


def csrf_failed() -> AppError:
    return AppError("forbidden", "Missing or invalid CSRF token.", status_code=403)


async def verify_login_request(request: Request, settings: Settings) -> None:
    """Origin check + the short-lived signed pre-login token (login CSRF)."""
    check_origin(request, settings)
    if not verify_login_token(settings, await submitted_csrf_token(request)):
        raise csrf_failed()


async def _authenticate(
    request: Request,
    settings: Settings,
    database: Database,
    db: AsyncSession,
    *,
    allow_pending: bool,
) -> SessionContext:
    token = get_session_token(request, settings)
    ctx = await auth.resolve_session(db, token)
    if ctx is None:
        raise unauthorized()
    if request.method not in SAFE_METHODS:
        check_origin(request, settings)
        submitted = await submitted_csrf_token(request)
        if not submitted or not constant_time_equals(submitted, ctx.session.csrf_token):
            raise csrf_failed()
    if ctx.user.must_change_password and not allow_pending:
        raise AppError(
            PASSWORD_CHANGE_REQUIRED,
            "You must change your password before continuing.",
            status_code=403,
        )
    if request.method in SAFE_METHODS:
        async with database.session() as write_db:  # the request session is read-only
            await auth.touch_session(write_db, ctx.session.id, ctx.session.last_seen_at)
    else:  # already inside BEGIN IMMEDIATE: a second writer connection would deadlock
        await auth.touch_session(db, ctx.session.id, ctx.session.last_seen_at)
    request.state.csrf_token = ctx.session.csrf_token
    request.state.user = ctx.user
    return ctx


async def require_user(
    request: Request, settings: SettingsDep, database: DatabaseDep, db: DbDep
) -> SessionContext:
    return await _authenticate(request, settings, database, db, allow_pending=False)


async def require_user_allow_pending(
    request: Request, settings: SettingsDep, database: DatabaseDep, db: DbDep
) -> SessionContext:
    """Like require_user but usable while must_change_password is set (change-password, logout)."""
    return await _authenticate(request, settings, database, db, allow_pending=True)


CurrentUser = Annotated[SessionContext, Depends(require_user)]
CurrentUserAllowPending = Annotated[SessionContext, Depends(require_user_allow_pending)]


def require_role(*roles: Role) -> Callable[..., Awaitable[SessionContext]]:
    async def dependency(ctx: CurrentUser) -> SessionContext:
        if ctx.user.role not in roles:
            raise forbidden()
        return ctx

    return dependency
