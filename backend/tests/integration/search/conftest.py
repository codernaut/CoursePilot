"""Fixtures for the search API and page tests: the app uses the fake embedder, and the vector index
of the temp database is built for it. These shadow the default ``settings`` and ``app`` fixtures
(whose embedding provider has no key) for every test in this package."""

import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.ingestion import pipeline
from app.ingestion.embedder import FakeEmbedder
from app.ingestion.pipeline import PipelineDeps
from app.main import create_app
from app.models import Block, Document, Module, Year
from app.models.enums import DocumentStatus, Role
from tests.conftest import Actor, insert_document, insert_user, make_settings
from tests.ingest_support import DIM, MODEL, configure_fake_index

ActorFactory = Callable[..., Awaitable[Actor]]


def fake_embedder() -> FakeEmbedder:
    return FakeEmbedder(model=MODEL, dim=DIM)


@pytest.fixture
def settings(tmp_path: Path, template_db: Path) -> Settings:
    shutil.copyfile(template_db, tmp_path / "test.db")
    configure_fake_index(tmp_path / "test.db")
    return make_settings(
        tmp_path, embedding_provider="fake", embedding_model=MODEL, embedding_dim=DIM
    )


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(settings, embedder_factory=fake_embedder)
    yield application
    await application.state.database.dispose()


async def module_ids(app: FastAPI) -> dict[str, str]:
    """``"1.1.1"`` -> module id (year.block.module)."""
    from sqlalchemy import select

    async with app.state.database.read_session() as db:
        rows = (
            await db.execute(
                select(Year.number, Block.number, Module.number, Module.id)
                .join(Block, Block.year_id == Year.id)
                .join(Module, Module.block_id == Block.id)
            )
        ).all()
    return {f"{y}.{b}.{m}": mid for y, b, m, mid in rows}


async def ingest_markdown(
    app: FastAPI,
    settings: Settings,
    *,
    module_id: str,
    title: str,
    body: str,
    week: str | None = None,
) -> Document:
    """Store a Markdown document through the real pipeline (ends ``ready``)."""
    uploader = await insert_user(app, email=f"up-{title.lower()}@example.edu", role=Role.FACULTY)
    document = await insert_document(
        app,
        module_id=module_id,
        uploaded_by=uploader.id,
        content=body.encode(),
        filename=f"{title.lower()}.md",
        title=title,
        mime_type="text/markdown",
        status=DocumentStatus.UPLOADED,
    )
    if week is not None:
        async with app.state.database.session() as db:
            row = await db.get(Document, document.id)
            assert row is not None
            row.week = week
            await db.commit()
    deps = PipelineDeps(
        database=app.state.database,
        storage=app.state.storage,
        settings=settings,
        embedder_factory=fake_embedder,
    )
    await pipeline.process_document(deps, document.id)
    return document
