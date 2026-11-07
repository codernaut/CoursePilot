"""Helpers for pipeline/worker tests: a migrated temp database whose vector index uses the fake
embedder's dimension, local storage in ``tmp_path``, a seeded programme and a faculty user."""

import hashlib
import shutil
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import sqlite_vec
from sqlalchemy import func, select, text

from app.core.config import Settings
from app.db.session import Database
from app.ingestion.embedder import FakeEmbedder
from app.ingestion.pipeline import PipelineDeps
from app.jobs import enqueue
from app.models import Block, Chunk, Document, Module, User, Year
from app.models.enums import DocumentStatus, JobType, Role
from app.services import structure
from app.storage import LocalStorage, new_key
from tests.conftest import make_settings

DIM = 16
MODEL = "fake-hash-v1"


def configure_fake_index(db_path: Path, dim: int = DIM, model: str = MODEL) -> None:
    """Point an existing migrated database at the fake embedder (vector table + app_meta)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.execute("DROP TABLE chunk_vectors")
        conn.execute(
            f"CREATE VIRTUAL TABLE chunk_vectors USING vec0(embedding float[{dim}] "
            "distance_metric=cosine)"
        )
        for key, value in (
            ("embedding_provider", "fake"),
            ("embedding_model", model),
            ("embedding_dim", str(dim)),
        ):
            conn.execute("UPDATE app_meta SET value = ? WHERE key = ?", (value, key))
        conn.commit()
    finally:
        conn.close()


@dataclass
class Env:
    settings: Settings
    database: Database
    storage: LocalStorage
    embedder: FakeEmbedder
    deps: PipelineDeps
    module_id: str
    user_id: str

    async def add_document(
        self,
        data: bytes,
        filename: str = "notes.md",
        *,
        title: str | None = None,
        status: DocumentStatus = DocumentStatus.UPLOADED,
        job: JobType | None = None,
        week: str | None = None,
    ) -> Document:
        key = new_key()

        async def _body() -> AsyncIterator[bytes]:
            yield data

        await self.storage.put(key, _body())
        async with self.database.session() as db:
            document = Document(
                module_id=self.module_id,
                uploaded_by=self.user_id,
                title=title or Path(filename).stem,
                original_filename=filename,
                mime_type="application/octet-stream",
                size_bytes=len(data),
                storage_key=key,
                content_sha256=hashlib.sha256(data).hexdigest(),
                status=status,
                week=week,
            )
            db.add(document)
            await db.flush()
            if job is not None:
                enqueue(db, job, {"document_id": document.id})
            await db.commit()
            return document

    async def document(self, document_id: str) -> Document | None:
        async with self.database.read_session() as db:
            return await db.get(Document, document_id)

    async def chunks(self, document_id: str) -> list[Chunk]:
        async with self.database.read_session() as db:
            rows = await db.execute(
                select(Chunk).where(Chunk.document_id == document_id).order_by(Chunk.ordinal)
            )
            return list(rows.scalars().all())

    async def count(self, table: str) -> int:
        async with self.database.read_session() as db:
            return int((await db.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())

    async def close(self) -> None:
        await self.database.dispose()


async def build_env(tmp_path: Path, template_db: Path, **overrides: object) -> Env:
    shutil.copyfile(template_db, tmp_path / "test.db")
    values: dict[str, object] = {
        "embedding_provider": "fake",
        "embedding_model": MODEL,
        "embedding_dim": DIM,
    }
    values.update(overrides)
    settings = make_settings(tmp_path, **values)
    configure_fake_index(
        tmp_path / "test.db", int(str(values["embedding_dim"])), str(values["embedding_model"])
    )
    database = Database(settings.database_url)
    storage = LocalStorage(settings.data_dir)
    async with database.session() as db:
        await structure.seed_structure(
            db,
            years=settings.program_years,
            blocks_per_year=settings.blocks_per_year,
            modules_per_block=settings.modules_per_block,
        )
        user = User(
            email="faculty@example.edu",
            display_name="Faculty",
            role=Role.FACULTY,
            password_hash="x",
            is_active=True,
            must_change_password=False,
        )
        db.add(user)
        module = (
            await db.execute(
                select(Module)
                .join(Block, Module.block_id == Block.id)
                .join(Year, Block.year_id == Year.id)
                .where(Year.number == 1, Block.number == 1, Module.number == 1)
            )
        ).scalar_one_or_none()
        assert module is not None
        await db.commit()
        module_id, user_id = module.id, user.id
    embedder = FakeEmbedder(
        model=str(values["embedding_model"]), dim=int(str(values["embedding_dim"]))
    )
    deps = PipelineDeps(
        database=database,
        storage=storage,
        settings=settings,
        embedder_factory=lambda: embedder,
    )
    return Env(settings, database, storage, embedder, deps, module_id, user_id)


async def chunk_count(database: Database) -> int:
    async with database.read_session() as db:
        return int((await db.execute(select(func.count()).select_from(Chunk))).scalar_one())
