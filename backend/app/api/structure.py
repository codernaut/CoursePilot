from fastapi import APIRouter, Depends

from app.api.deps import CurrentUser, DbDep, require_role
from app.models.enums import Role
from app.schemas.structure import (
    BlockOut,
    ModuleDetail,
    ModuleOut,
    ModulePatch,
    StructureOut,
    TitlePatch,
    YearOut,
)
from app.services import structure
from app.services.auth import SessionContext

router = APIRouter(tags=["structure"])

AdminDep = Depends(require_role(Role.ADMIN))
EditorDep = Depends(require_role(Role.ADMIN, Role.FACULTY))


@router.get("/structure", response_model=StructureOut)
async def get_structure(db: DbDep, _: CurrentUser) -> StructureOut:
    years = await structure.get_tree(db)
    return StructureOut(years=[YearOut.model_validate(y) for y in years])


@router.patch("/years/{year_id}", response_model=YearOut)
async def patch_year(
    year_id: str, body: TitlePatch, db: DbDep, ctx: SessionContext = AdminDep
) -> YearOut:
    return YearOut.model_validate(await structure.rename_year(db, ctx.user, year_id, body.title))


@router.patch("/blocks/{block_id}", response_model=BlockOut)
async def patch_block(
    block_id: str, body: TitlePatch, db: DbDep, ctx: SessionContext = AdminDep
) -> BlockOut:
    return BlockOut.model_validate(await structure.rename_block(db, ctx.user, block_id, body.title))


@router.get("/modules/{module_id}", response_model=ModuleDetail)
async def get_module(module_id: str, db: DbDep, _: CurrentUser) -> ModuleDetail:
    return ModuleDetail.model_validate(await structure.get_module(db, module_id))


@router.patch("/modules/{module_id}", response_model=ModuleOut)
async def patch_module(
    module_id: str, body: ModulePatch, db: DbDep, ctx: SessionContext = EditorDep
) -> ModuleOut:
    module = await structure.update_module(
        db,
        ctx.user,
        module_id,
        title=body.title,
        description=body.description,
        web_search_enabled=body.web_search_enabled,
    )
    return ModuleOut.model_validate(module)
