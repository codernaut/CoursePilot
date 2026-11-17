from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import Role


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    display_name: str
    role: Role
    is_active: bool
    must_change_password: bool


class LoginRequest(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=1024)


class LoginResponse(BaseModel):
    user: UserOut
    csrf_token: str


class GuestInfo(BaseModel):
    module_id: str
    module_title: str
    expires_at: datetime


class MeResponse(BaseModel):
    user: UserOut
    csrf_token: str
    guest: GuestInfo | None = None  # set for guests (temporary module access)


class GuestLoginRequest(BaseModel):
    code: str = Field(max_length=64)
    name: str = Field(max_length=200)


class GuestLoginResponse(BaseModel):
    user: UserOut
    csrf_token: str
    guest: GuestInfo


class LoginTokenResponse(BaseModel):
    csrf_token: str


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(max_length=1024)
    new_password: str = Field(max_length=1024)


class OkResponse(BaseModel):
    ok: bool = True
