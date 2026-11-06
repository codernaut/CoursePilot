from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, BaseModel


class Year(BaseModel, Base):
    __tablename__ = "years"

    number: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)

    blocks: Mapped[list["Block"]] = relationship(
        back_populates="year", order_by="Block.number", lazy="selectin"
    )


class Block(BaseModel, Base):
    __tablename__ = "blocks"
    __table_args__ = (UniqueConstraint("year_id", "number"),)

    year_id: Mapped[str] = mapped_column(ForeignKey("years.id"), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)

    year: Mapped[Year] = relationship(back_populates="blocks", lazy="joined")
    modules: Mapped[list["Module"]] = relationship(
        back_populates="block", order_by="Module.number", lazy="selectin"
    )


class Module(BaseModel, Base):
    __tablename__ = "modules"
    __table_args__ = (UniqueConstraint("block_id", "number"),)

    block_id: Mapped[str] = mapped_column(ForeignKey("blocks.id"), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    web_search_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    block: Mapped[Block] = relationship(back_populates="modules", lazy="joined")
