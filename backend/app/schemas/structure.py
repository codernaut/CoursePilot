from pydantic import BaseModel, ConfigDict, Field


class ModuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    number: int
    title: str
    description: str
    web_search_enabled: bool


class BlockOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    number: int
    title: str
    modules: list[ModuleOut]


class YearOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    number: int
    title: str
    blocks: list[BlockOut]


class StructureOut(BaseModel):
    years: list[YearOut]


class TitlePatch(BaseModel):
    title: str = Field(max_length=500)


class ModulePatch(BaseModel):
    title: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=10000)
    web_search_enabled: bool | None = None


class ModuleDetail(ModuleOut):
    block_id: str
