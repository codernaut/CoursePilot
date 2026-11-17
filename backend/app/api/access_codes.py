"""Temporary module access codes (faculty and administrators). Guests redeem them at
``POST /auth/guest``."""

from fastapi import APIRouter, Depends, Response

from app.api.deps import DbDep, require_role
from app.models import User
from app.models.enums import Role
from app.schemas.access_codes import AccessCodeCreate, AccessCodeCreated, AccessCodeOut
from app.services import access_codes
from app.services.access_codes import CodeView
from app.services.auth import SessionContext

router = APIRouter(tags=["access-codes"])

IssuerDep = Depends(require_role(Role.ADMIN, Role.FACULTY))


@router.post("/modules/{module_id}/access-codes", response_model=AccessCodeCreated, status_code=201)
async def create_access_code(
    module_id: str,
    body: AccessCodeCreate,
    response: Response,
    db: DbDep,
    ctx: SessionContext = IssuerDep,
) -> AccessCodeCreated:
    """Create a code for the module, valid from now for 2, 4, 8, 10, 12 or 24 hours. The plain
    code is in this response only; it cannot be retrieved later (revoke it and create a new one)."""
    code, plain = await access_codes.create_code(
        db, ctx.user, module_id, duration_hours=body.duration_hours, label=body.label
    )
    response.headers["Cache-Control"] = "no-store"  # the body holds the plain code
    view = CodeView(code, "active", ctx.user.display_name)
    return AccessCodeCreated(**AccessCodeOut.from_view(view).model_dump(), code=plain)


@router.get("/modules/{module_id}/access-codes", response_model=list[AccessCodeOut])
async def list_access_codes(
    module_id: str, db: DbDep, ctx: SessionContext = IssuerDep
) -> list[AccessCodeOut]:
    """Active codes and recently ended ones, newest first (never the plain codes)."""
    views = await access_codes.list_codes(db, ctx.user, module_id)
    return [AccessCodeOut.from_view(v) for v in views]


@router.post("/access-codes/{code_id}/revoke", response_model=AccessCodeOut)
async def revoke_access_code(
    code_id: str, db: DbDep, ctx: SessionContext = IssuerDep
) -> AccessCodeOut:
    """End the code now: every guest who joined with it is signed out immediately."""
    code = await access_codes.revoke_code(db, ctx.user, code_id)
    creator = await db.get(User, code.created_by)
    return AccessCodeOut.from_view(
        CodeView(code, "revoked", creator.display_name if creator else "")
    )
