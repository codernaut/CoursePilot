from fastapi import APIRouter, Depends

from app.api.deps import DbDep, require_role
from app.models.enums import Role
from app.schemas.auth import UserOut
from app.schemas.users import ResetPasswordResponse, UserCreate, UserCreated, UserPatch
from app.services import users
from app.services.auth import SessionContext

router = APIRouter(prefix="/users", tags=["users"])

AdminDep = Depends(require_role(Role.ADMIN))


@router.get("", response_model=list[UserOut])
async def list_users(db: DbDep, ctx: SessionContext = AdminDep) -> list[UserOut]:
    return [UserOut.model_validate(u) for u in await users.list_users(db, ctx.user)]


@router.post("", response_model=UserCreated, status_code=201)
async def create_user(body: UserCreate, db: DbDep, ctx: SessionContext = AdminDep) -> UserCreated:
    user, temporary = await users.create_user(
        db,
        ctx.user,
        email=body.email,
        display_name=body.display_name,
        role=body.role,
        password=body.password,
    )
    return UserCreated(user=UserOut.model_validate(user), temporary_password=temporary)


@router.get("/{user_id}", response_model=UserOut)
async def get_user(user_id: str, db: DbDep, ctx: SessionContext = AdminDep) -> UserOut:
    return UserOut.model_validate(await users.get_user(db, ctx.user, user_id))


@router.patch("/{user_id}", response_model=UserOut)
async def patch_user(
    user_id: str, body: UserPatch, db: DbDep, ctx: SessionContext = AdminDep
) -> UserOut:
    user = await users.update_user(
        db,
        ctx.user,
        user_id,
        display_name=body.display_name,
        role=body.role,
        is_active=body.is_active,
    )
    return UserOut.model_validate(user)


@router.post("/{user_id}/reset-password", response_model=ResetPasswordResponse)
async def reset_password(
    user_id: str, db: DbDep, ctx: SessionContext = AdminDep
) -> ResetPasswordResponse:
    user, temporary = await users.reset_password(db, ctx.user, user_id)
    return ResetPasswordResponse(user=UserOut.model_validate(user), temporary_password=temporary)
