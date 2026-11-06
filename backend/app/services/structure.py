"""Programme structure: seeding and renaming Years, Blocks and Modules."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import forbidden, not_found, validation_error
from app.models import Block, Module, User, Year
from app.models.enums import Role
from app.services import audit

_MAX_TITLE = 120
_MAX_DESCRIPTION = 2000


@dataclass(frozen=True)
class SeedResult:
    years: int
    blocks: int
    modules: int

    @property
    def total(self) -> int:
        return self.years + self.blocks + self.modules


async def seed_structure(
    db: AsyncSession, *, years: int, blocks_per_year: int, modules_per_block: int
) -> SeedResult:
    """Create any missing years/blocks/modules. Never renames, reorders or deletes."""
    created_years = created_blocks = created_modules = 0
    for y in range(1, years + 1):
        year = (await db.execute(select(Year).where(Year.number == y))).scalar_one_or_none()
        if year is None:
            year = Year(number=y, title=f"Year {y}")
            db.add(year)
            await db.flush()
            created_years += 1
        for b in range(1, blocks_per_year + 1):
            block = (
                await db.execute(select(Block).where(Block.year_id == year.id, Block.number == b))
            ).scalar_one_or_none()
            if block is None:
                block = Block(year_id=year.id, number=b, title=f"Block {b}")
                db.add(block)
                await db.flush()
                created_blocks += 1
            for m in range(1, modules_per_block + 1):
                found = (
                    await db.execute(
                        select(Module.id).where(Module.block_id == block.id, Module.number == m)
                    )
                ).first()
                if found is None:
                    db.add(Module(block_id=block.id, number=m, title=f"Module {m}"))
                    created_modules += 1
    await db.commit()
    return SeedResult(created_years, created_blocks, created_modules)


async def get_tree(db: AsyncSession) -> list[Year]:
    return list((await db.execute(select(Year).order_by(Year.number))).scalars().all())


async def get_module(db: AsyncSession, module_id: str) -> Module:
    module = await db.get(Module, module_id)
    if module is None:
        raise not_found("Module not found.")
    return module


def _clean_title(title: str) -> str:
    cleaned = " ".join(title.split())
    if not cleaned or len(cleaned) > _MAX_TITLE:
        raise validation_error(f"Title must be 1-{_MAX_TITLE} characters.")
    return cleaned


def _require(actor: User, *roles: Role) -> None:
    if actor.role not in roles or not actor.is_active:
        raise forbidden()


async def rename_year(db: AsyncSession, actor: User, year_id: str, title: str) -> Year:
    _require(actor, Role.ADMIN)
    year = await db.get(Year, year_id)
    if year is None:
        raise not_found("Year not found.")
    new_title = _clean_title(title)
    if new_title != year.title:
        audit.record(
            db,
            "structure_renamed",
            actor_id=actor.id,
            target_type="year",
            target_id=year.id,
            details={"from": year.title, "to": new_title},
        )
        year.title = new_title
    await db.commit()
    return year


async def rename_block(db: AsyncSession, actor: User, block_id: str, title: str) -> Block:
    _require(actor, Role.ADMIN)
    block = await db.get(Block, block_id)
    if block is None:
        raise not_found("Block not found.")
    new_title = _clean_title(title)
    if new_title != block.title:
        audit.record(
            db,
            "structure_renamed",
            actor_id=actor.id,
            target_type="block",
            target_id=block.id,
            details={"from": block.title, "to": new_title},
        )
        block.title = new_title
    await db.commit()
    return block


async def update_module(
    db: AsyncSession,
    actor: User,
    module_id: str,
    *,
    title: str | None = None,
    description: str | None = None,
    web_search_enabled: bool | None = None,
) -> Module:
    _require(actor, Role.ADMIN, Role.FACULTY)
    module = await get_module(db, module_id)
    details: dict[str, object] = {}
    if title is not None:
        new_title = _clean_title(title)
        if new_title != module.title:
            details["title"] = {"from": module.title, "to": new_title}
            module.title = new_title
    if description is not None:
        cleaned = description.strip()
        if len(cleaned) > _MAX_DESCRIPTION:
            raise validation_error(f"Description must be at most {_MAX_DESCRIPTION} characters.")
        if cleaned != module.description:
            details["description_changed"] = True
            module.description = cleaned
    if web_search_enabled is not None and web_search_enabled != module.web_search_enabled:
        details["web_search_enabled"] = web_search_enabled
        module.web_search_enabled = web_search_enabled
    if details:
        audit.record(
            db,
            "structure_renamed" if "title" in details else "module_updated",
            actor_id=actor.id,
            target_type="module",
            target_id=module.id,
            details=details,
        )
    await db.commit()
    return module
