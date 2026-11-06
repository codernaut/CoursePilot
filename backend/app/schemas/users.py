from pydantic import BaseModel, Field

from app.models.enums import Role
from app.schemas.auth import UserOut


class UserCreate(BaseModel):
    email: str = Field(max_length=254)
    display_name: str = Field(max_length=200)
    role: Role
    password: str | None = Field(default=None, max_length=1024)


class UserCreated(BaseModel):
    user: UserOut
    temporary_password: str | None = None


class UserPatch(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)
    role: Role | None = None
    is_active: bool | None = None


class ResetPasswordResponse(BaseModel):
    user: UserOut
    temporary_password: str
