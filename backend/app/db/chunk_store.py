"""SQL for the chunk index: ``chunks`` plus its two virtual tables ``chunk_vectors`` (sqlite-vec)
and ``chunks_fts`` (FTS5), which share ``chunks.seq`` as rowid (CLAUDE.md section 5).

Every function takes the caller's session and never commits, so the pipeline can replace a
document's chunks, vectors and FTS rows in ONE transaction.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import sqlite_vec
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppMeta, Chunk

META_PROVIDER = "embedding_provider"
META_MODEL = "embedding_model"
META_DIM = "embedding_dim"


@dataclass(frozen=True)
class EmbeddingConfig:
    """What ``chunk_vectors`` was built with (``app_meta``) or what Settings asks for."""

    provider: str
    model: str
    dim: int

    def describe(self) -> str:
        return f"{self.provider} / {self.model} / {self.dim} dimensions"


async def read_embedding_config(db: AsyncSession) -> EmbeddingConfig | None:
    rows = (
        await db.execute(
            select(AppMeta.key, AppMeta.value).where(
                AppMeta.key.in_([META_PROVIDER, META_MODEL, META_DIM])
            )
        )
    ).all()
    values = {key: value for key, value in rows}
    if len(values) != 3 or not values[META_DIM].isdigit():
        return None
    return EmbeddingConfig(values[META_PROVIDER], values[META_MODEL], int(values[META_DIM]))


async def write_embedding_config(db: AsyncSession, config: EmbeddingConfig) -> None:
    for key, value in (
        (META_PROVIDER, config.provider),
        (META_MODEL, config.model),
        (META_DIM, str(config.dim)),
    ):
        row = await db.get(AppMeta, key)
        if row is None:
            db.add(AppMeta(key=key, value=value))
        else:
            row.value = value


async def recreate_vector_table(db: AsyncSession, dim: int) -> None:
    """Drop and recreate ``chunk_vectors`` with a new dimension (empty afterwards)."""
    await db.execute(text("DROP TABLE IF EXISTS chunk_vectors"))
    await db.execute(
        text(
            "CREATE VIRTUAL TABLE chunk_vectors USING vec0("
            f"embedding float[{int(dim)}] distance_metric=cosine)"
        )
    )


async def delete_document_index(db: AsyncSession, document_id: str) -> int:
    """Remove a document's chunks together with their vector and FTS rows. Returns chunk count."""
    params = {"document_id": document_id}
    subquery = "SELECT seq FROM chunks WHERE document_id = :document_id"
    await db.execute(text(f"DELETE FROM chunk_vectors WHERE rowid IN ({subquery})"), params)  # noqa: S608
    await db.execute(text(f"DELETE FROM chunks_fts WHERE rowid IN ({subquery})"), params)  # noqa: S608
    result = await db.execute(delete(Chunk).where(Chunk.document_id == document_id))
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def insert_chunks(
    db: AsyncSession, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]
) -> None:
    """Insert chunks and write their vectors and FTS rows under the generated ``seq``."""
    if len(chunks) != len(vectors):
        raise ValueError("one vector per chunk is required")
    db.add_all(chunks)
    await db.flush()  # assigns chunk.seq
    vector_rows: list[dict[str, Any]] = []
    fts_rows: list[dict[str, Any]] = []
    for chunk, vector in zip(chunks, vectors, strict=True):
        vector_rows.append(
            {"rowid": int(chunk.seq), "embedding": sqlite_vec.serialize_float32(vector)}
        )
        fts_rows.append({"rowid": int(chunk.seq), "embed_text": chunk.embed_text})
    if vector_rows:
        await db.execute(
            text("INSERT INTO chunk_vectors(rowid, embedding) VALUES (:rowid, :embedding)"),
            vector_rows,
        )
        await db.execute(
            text("INSERT INTO chunks_fts(rowid, embed_text) VALUES (:rowid, :embed_text)"),
            fts_rows,
        )


async def count_chunks(db: AsyncSession, document_ids: Sequence[str]) -> dict[str, int]:
    """Chunk count per document id (documents without chunks are absent)."""
    if not document_ids:
        return {}
    rows = (
        await db.execute(
            select(Chunk.document_id, func.count())
            .where(Chunk.document_id.in_(list(document_ids)))
            .group_by(Chunk.document_id)
        )
    ).all()
    return {doc_id: int(count) for doc_id, count in rows}
