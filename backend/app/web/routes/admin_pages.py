"""Admin user management page."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from starlette.responses import Response

from app.api.deps import DbDep, require_role
from app.core.errors import AppError, validation_error
from app.models.enums import Role
from app.services import users
from app.services.auth import SessionContext
from app.web.common import render

router = APIRouter(prefix="/admin", include_in_schema=False)

AdminDep = Depends(require_role(Role.ADMIN))


def _role(value: str) -> Role:
    try:
        return Role(value)
    except ValueError:
        raise validation_error("Choose a valid role.") from None


async def _users_page(
    request: Request,
    db: DbDep,
    ctx: SessionContext,
    *,
    error: str | None = None,
    notice: str | None = None,
    temporary: tuple[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    return render(
        request,
        "admin_users.html",
        {
            "users": await users.list_users(db, ctx.user),
            "roles": list(Role),
            "error": error,
            "notice": notice,
            "temporary": temporary,
        },
        status_code=status_code,
    )


@router.get("/users")
async def users_page(request: Request, db: DbDep, ctx: SessionContext = AdminDep) -> Response:
    return await _users_page(request, db, ctx)


@router.post("/users")
async def create_user(
    request: Request,
    db: DbDep,
    ctx: SessionContext = AdminDep,
    email: Annotated[str, Form()] = "",
    display_name: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "student",
    password: Annotated[str, Form()] = "",
) -> Response:
    try:
        user, generated = await users.create_user(
            db,
            ctx.user,
            email=email[:254],
            display_name=display_name[:300],
            role=_role(role),
            password=password[:1024] or None,
        )
    except AppError as exc:
        return await _users_page(request, db, ctx, error=exc.message, status_code=exc.status_code)
    temporary = (user.email, generated) if generated else None
    return await _users_page(request, db, ctx, notice=f"Created {user.email}.", temporary=temporary)


@router.post("/users/{user_id}/role")
async def change_role(
    request: Request,
    user_id: str,
    db: DbDep,
    ctx: SessionContext = AdminDep,
    role: Annotated[str, Form()] = "",
) -> Response:
    try:
        user = await users.update_user(db, ctx.user, user_id, role=_role(role))
    except AppError as exc:
        return await _users_page(request, db, ctx, error=exc.message, status_code=exc.status_code)
    return await _users_page(request, db, ctx, notice=f"Updated the role of {user.email}.")


@router.post("/users/{user_id}/active")
async def set_active(
    request: Request,
    user_id: str,
    db: DbDep,
    ctx: SessionContext = AdminDep,
    active: Annotated[str, Form()] = "",
) -> Response:
    try:
        user = await users.update_user(db, ctx.user, user_id, is_active=active == "1")
    except AppError as exc:
        return await _users_page(request, db, ctx, error=exc.message, status_code=exc.status_code)
    state = "reactivated" if user.is_active else "deactivated"
    return await _users_page(request, db, ctx, notice=f"{user.email} {state}.")


@router.post("/users/{user_id}/reset-password")
async def reset_password(
    request: Request, user_id: str, db: DbDep, ctx: SessionContext = AdminDep
) -> Response:
    try:
        user, temporary = await users.reset_password(db, ctx.user, user_id)
    except AppError as exc:
        return await _users_page(request, db, ctx, error=exc.message, status_code=exc.status_code)
    return await _users_page(
        request,
        db,
        ctx,
        notice=f"Password reset for {user.email}.",
        temporary=(user.email, temporary),
    )
