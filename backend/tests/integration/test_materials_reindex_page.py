"""The materials list shows re-indexing and a failed re-index without hiding the document."""

from collections.abc import Awaitable, Callable

import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from app.models import Document, Module
from app.models.enums import Role
from tests.conftest import Actor, insert_document

ActorFactory = Callable[..., Awaitable[Actor]]


@pytest.fixture
async def module(app: FastAPI, seeded: None) -> Module:
    async with app.state.database.read_session() as db:
        found = (await db.execute(select(Module).order_by(Module.number))).scalars().first()
    assert isinstance(found, Module)
    return found


async def set_fields(app: FastAPI, document_id: str, **values: object) -> None:
    async with app.state.database.session() as db:
        await db.execute(update(Document).where(Document.id == document_id).values(**values))
        await db.commit()


async def test_a_reindexing_document_is_shown_ready_and_the_list_keeps_polling(
    app: FastAPI, make_actor: ActorFactory, module: Module
) -> None:
    faculty = await make_actor(Role.FACULTY)
    doc = await insert_document(app, module_id=module.id, uploaded_by=faculty.user.id)
    await set_fields(app, doc.id, reindexing=True)
    html = (await faculty.client.get(f"/modules/{module.id}/materials/documents")).text
    assert "Ready" in html and "Re-indexing" in html
    assert 'hx-trigger="every 3s"' in html
    assert f"/materials/{doc.id}/reindex" not in html  # no second request while one is pending
    assert f"/materials/{doc.id}/delete" in html  # deleting is still possible


async def test_a_failed_reindex_shows_the_error_and_the_old_version_stays_listed(
    app: FastAPI, make_actor: ActorFactory, module: Module
) -> None:
    faculty = await make_actor(Role.FACULTY)
    doc = await insert_document(app, module_id=module.id, uploaded_by=faculty.user.id)
    await set_fields(app, doc.id, error="The embedding key <b>was rejected</b>.")
    html = (await faculty.client.get(f"/modules/{module.id}/materials/documents")).text
    assert "The last re-index failed" in html and "still searchable" in html
    assert "&lt;b&gt;was rejected&lt;/b&gt;" in html  # escaped
    assert 'hx-trigger="every 3s"' not in html
    assert f"/materials/{doc.id}/reindex" in html  # can be tried again
