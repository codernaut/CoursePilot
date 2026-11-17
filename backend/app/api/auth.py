from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from app.api.deps import (
    AnySession,
    CurrentMember,
    CurrentUserAllowPending,
    DbDep,
    SettingsDep,
    clear_session_cookie,
    client_ip,
    get_guest_throttle,
    get_password_throttle,
    get_session_token,
    get_throttle,
    set_session_cookie,
    verify_login_request,
)
from app.core.errors import unauthorized
from app.models import Module
from app.schemas.auth import (
    ChangePasswordRequest,
    GuestInfo,
    GuestLoginRequest,
    GuestLoginResponse,
    LoginRequest,
    LoginResponse,
    LoginTokenResponse,
    MeResponse,
    OkResponse,
    UserOut,
)
from app.services import access_codes, auth
from app.services.security import make_login_token

router = APIRouter(prefix="/auth", tags=["auth"])

ThrottleDep = Annotated[auth.LoginThrottle, Depends(get_throttle)]
GuestThrottleDep = Annotated[auth.LoginThrottle, Depends(get_guest_throttle)]
PasswordThrottleDep = Annotated[auth.LoginThrottle, Depends(get_password_throttle)]


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
    response: Response, settings: SettingsDep, db: DbDep, ctx: AnySession
) -> OkResponse:
    await auth.logout(db, ctx)
    clear_session_cookie(response, settings)
    return OkResponse()


@router.get("/me", response_model=MeResponse)
async def me(db: DbDep, ctx: CurrentMember) -> MeResponse:
    return MeResponse(
        user=UserOut.model_validate(ctx.user),
        csrf_token=ctx.session.csrf_token,
        guest=await guest_info(db, ctx),
    )


async def guest_info(db: DbDep, ctx: auth.SessionContext) -> GuestInfo | None:
    if ctx.access_code is None:
        return None
    module = await db.get(Module, ctx.access_code.module_id)
    return GuestInfo(
        module_id=ctx.access_code.module_id,
        module_title=module.title if module else "",
        expires_at=ctx.access_code.expires_at,
    )


@router.post("/guest", response_model=GuestLoginResponse)
async def guest_login(
    body: GuestLoginRequest,
    request: Request,
    response: Response,
    settings: SettingsDep,
    db: DbDep,
    throttle: GuestThrottleDep,
) -> GuestLoginResponse:
    """Join a module with a temporary access code (no account). Same pre-login CSRF token and
    Origin check as ``/auth/login``. Unknown, expired and revoked codes give the same 401."""
    await verify_login_request(request, settings)
    result = await access_codes.redeem(
        db,
        settings,
        throttle,
        code=body.code,
        name=body.name,
        ip=client_ip(request, settings),
        user_agent=request.headers.get("user-agent"),
        old_token=get_session_token(request, settings),
    )
    set_session_cookie(response, settings, result.token)
    code = await access_codes.get_guest_code(db, result.user.id)
    info = await guest_info(db, auth.SessionContext(result.session, result.user, code))
    if info is None:  # pragma: no cover - redeem always links the code
        raise unauthorized()
    return GuestLoginResponse(
        user=UserOut.model_validate(result.user),
        csrf_token=result.session.csrf_token,
        guest=info,
    )


@router.post("/change-password", response_model=OkResponse)
async def change_password(
    body: ChangePasswordRequest,
    db: DbDep,
    ctx: CurrentUserAllowPending,
    throttle: PasswordThrottleDep,
) -> OkResponse:
    await auth.change_password(
        db,
        ctx,
        throttle,
        current_password=body.current_password,
        new_password=body.new_password,
    )
    return OkResponse()
