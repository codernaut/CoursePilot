from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

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
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
    LoginTokenResponse,
    MeResponse,
    OkResponse,
    UserOut,
)
from app.services import auth
from app.services.security import make_login_token

router = APIRouter(prefix="/auth", tags=["auth"])

ThrottleDep = Annotated[auth.LoginThrottle, Depends(get_throttle)]


@router.get("/csrf", response_model=LoginTokenResponse)
async def login_token(settings: SettingsDep) -> LoginTokenResponse:
    """Short-lived signed token that API clients send as X-CSRF-Token with POST /auth/login."""
    return LoginTokenResponse(csrf_token=make_login_token(settings))


@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    settings: SettingsDep,
    db: DbDep,
    throttle: ThrottleDep,
) -> LoginResponse:
    await verify_login_request(request, settings)
    result = await auth.login(
        db,
        settings,
        throttle,
        email=body.email,
        password=body.password,
        ip=client_ip(request, settings),
        user_agent=request.headers.get("user-agent"),
        old_token=get_session_token(request, settings),
    )
    set_session_cookie(response, settings, result.token)
    return LoginResponse(
        user=UserOut.model_validate(result.user), csrf_token=result.session.csrf_token
    )


@router.post("/logout", response_model=OkResponse)
async def logout(
    response: Response, settings: SettingsDep, db: DbDep, ctx: CurrentUserAllowPending
) -> OkResponse:
    await auth.logout(db, ctx)
    clear_session_cookie(response, settings)
    return OkResponse()


@router.get("/me", response_model=MeResponse)
async def me(ctx: CurrentUser) -> MeResponse:
    return MeResponse(user=UserOut.model_validate(ctx.user), csrf_token=ctx.session.csrf_token)


@router.post("/change-password", response_model=OkResponse)
async def change_password(
    body: ChangePasswordRequest, db: DbDep, ctx: CurrentUserAllowPending
) -> OkResponse:
    await auth.change_password(
        db, ctx, current_password=body.current_password, new_password=body.new_password
    )
    return OkResponse()
