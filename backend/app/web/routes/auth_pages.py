"""Login, logout, change-password and home pages."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from starlette.responses import RedirectResponse, Response

from app.api.deps import (
    CurrentUser,
    CurrentUserAllowPending,
    DbDep,
    SettingsDep,
    clear_session_cookie,
    client_ip,
    get_session_token,
    get_throttle,
    set_session_cookie,
    verify_login_request,
)
from app.core.errors import AppError
from app.services import auth, structure
from app.services.security import make_login_token
from app.web.common import render

router = APIRouter(include_in_schema=False)

ThrottleDep = Annotated[auth.LoginThrottle, Depends(get_throttle)]


@router.get("/login")
async def login_page(request: Request, settings: SettingsDep) -> Response:
    return render(request, "login.html", {"csrf_token": make_login_token(settings), "error": None})


@router.post("/login")
async def login_submit(
    request: Request,
    settings: SettingsDep,
    db: DbDep,
    throttle: ThrottleDep,
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
) -> Response:
    await verify_login_request(request, settings)
    try:
        result = await auth.login(
            db,
            settings,
            throttle,
            email=email[:254],
            password=password[:1024],
            ip=client_ip(request, settings),
            user_agent=request.headers.get("user-agent"),
            old_token=get_session_token(request, settings),
        )
    except AppError as exc:
        # One generic message for every credential failure (unknown, wrong password, inactive).
        return render(
            request,
            "login.html",
            {"csrf_token": make_login_token(settings), "error": exc.message, "email": email[:254]},
            status_code=exc.status_code,
        )
    target = "/change-password" if result.user.must_change_password else "/"
    response = RedirectResponse(target, status_code=303)
    set_session_cookie(response, settings, result.token)
    return response


@router.post("/logout")
async def logout_submit(settings: SettingsDep, db: DbDep, ctx: CurrentUserAllowPending) -> Response:
    await auth.logout(db, ctx)
    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(response, settings)
    return response


@router.get("/change-password")
async def change_password_page(request: Request, ctx: CurrentUserAllowPending) -> Response:
    return render(
        request, "change_password.html", {"error": None, "forced": ctx.user.must_change_password}
    )


@router.post("/change-password")
async def change_password_submit(
    request: Request,
    db: DbDep,
    ctx: CurrentUserAllowPending,
    current_password: Annotated[str, Form()] = "",
    new_password: Annotated[str, Form()] = "",
    confirm_password: Annotated[str, Form()] = "",
) -> Response:
    forced = ctx.user.must_change_password
    error: str | None = None
    status = 200
    if new_password != confirm_password:
        error, status = "The new passwords do not match.", 422
    else:
        try:
            await auth.change_password(
                db,
                ctx,
                current_password=current_password[:1024],
                new_password=new_password[:1024],
            )
        except AppError as exc:
            error, status = exc.message, exc.status_code
    if error is not None:
        return render(
            request, "change_password.html", {"error": error, "forced": forced}, status_code=status
        )
    return RedirectResponse("/?password_changed=1", status_code=303)


@router.get("/")
async def home(request: Request, db: DbDep, ctx: CurrentUser) -> Response:
    years = await structure.get_tree(db)
    return render(
        request,
        "home.html",
        {"years": years, "password_changed": request.query_params.get("password_changed") == "1"},
    )
