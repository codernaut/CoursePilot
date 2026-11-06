from typing import Any

from sqlalchemy import JSON, BigInteger, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, BaseModel, TimestampMixin, enum_column, new_id
from app.models.enums import DocumentStatus


class Document(BaseModel, Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("module_id", "content_sha256"),
        Index("ix_documents_module_id", "module_id"),
    )

    module_id: Mapped[str] = mapped_column(ForeignKey("modules.id"), nullable=False)
    uploaded_by: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(300), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(64), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[DocumentStatus] = mapped_column(
        enum_column(DocumentStatus, "document_status"),
        default=DocumentStatus.UPLOADED,
        nullable=False,
    )
    error: Mapped[str | None] = mapped_column(Text)
    page_count: Mapped[int | None] = mapped_column(Integer)
    week: Mapped[str | None] = mapped_column(String(60))


class Chunk(TimestampMixin, Base):
    """A retrievable passage. ``seq`` is the rowid shared with chunk_vectors and chunks_fts."""

    __tablename__ = "chunks"
    __table_args__ = (
        Index("ix_chunks_module_id", "module_id"),
        Index("ix_chunks_block_id", "block_id"),
        Index("ix_chunks_document_id_ordinal", "document_id", "ordinal"),
        {"sqlite_autoincrement": True},
    )

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(32), unique=True, default=new_id, nullable=False)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), nullable=False)
    module_id: Mapped[str] = mapped_column(ForeignKey("modules.id"), nullable=False)
    block_id: Mapped[str] = mapped_column(ForeignKey("blocks.id"), nullable=False)
    year_id: Mapped[str] = mapped_column(ForeignKey("years.id"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embed_text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    slide_number: Mapped[int | None] = mapped_column(Integer)
    section_path: Mapped[str | None] = mapped_column(Text)
    embedding_model: Mapped[str] = mapped_column(String(120), nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict, nullable=False)
