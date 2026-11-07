"""The materials page and documents API show processing results (errors, passages, pages)."""

from collections.abc import Awaitable, Callable

from fastapi import FastAPI
from sqlalchemy import select

from app.models import Block, Chunk, Document, Module
from app.models.enums import DocumentStatus, Role
from tests.conftest import Actor, insert_document

ActorFactory = Callable[..., Awaitable[Actor]]


async def _first_module(app: FastAPI) -> tuple[Module, Block]:
    async with app.state.database.read_session() as db:
        module = (await db.execute(select(Module).order_by(Module.number))).scalars().first()
        assert module is not None
        return module, module.block


async def _add_chunks(app: FastAPI, document_id: str, module: Module, block: Block, n: int) -> None:
    async with app.state.database.session() as db:
        for i in range(n):
            db.add(
                Chunk(
                    document_id=document_id,
                    module_id=module.id,
                    block_id=block.id,
                    year_id=block.year_id,
                    ordinal=i,
                    text="t",
                    embed_text="t",
                    token_count=1,
                    embedding_model="m",
                    meta={},
                )
            )
        await db.commit()


async def test_ready_documents_show_passage_and_page_counts(
    app: FastAPI, seeded: None, make_actor: ActorFactory
) -> None:
    faculty = await make_actor(Role.FACULTY)
    module, block = await _first_module(app)
    ready = await insert_document(
        app,
        module_id=module.id,
        uploaded_by=faculty.user.id,
        content=b"%PDF-1.4 a",
        title="Lecture",
    )
    single = await insert_document(
        app, module_id=module.id, uploaded_by=faculty.user.id, content=b"%PDF-1.4 b", title="One"
    )
    async with app.state.database.session() as db:
        for doc_id, pages in ((ready.id, 7), (single.id, 1)):
            row = await db.get(Document, doc_id)
            assert row is not None
            row.page_count = pages
        await db.commit()
    await _add_chunks(app, ready.id, module, block, 3)
    await _add_chunks(app, single.id, module, block, 1)

    html = (await faculty.client.get(f"/modules/{module.id}/materials/documents")).text
    assert "3 passages, 7 pages" in html
    assert "1 passage, 1 page" in html and "1 passages" not in html

    api = (await faculty.client.get(f"/api/v1/modules/{module.id}/documents")).json()
    by_id = {d["id"]: d for d in api}
    assert by_id[ready.id]["chunk_count"] == 3 and by_id[ready.id]["page_count"] == 7
    detail = (await faculty.client.get(f"/api/v1/documents/{ready.id}")).json()
    assert detail["chunk_count"] == 3


async def test_failed_documents_show_the_readable_error_and_no_counts(
    app: FastAPI, seeded: None, make_actor: ActorFactory
) -> None:
    faculty = await make_actor(Role.FACULTY)
    module, _ = await _first_module(app)
    doc = await insert_document(
        app,
        module_id=module.id,
        uploaded_by=faculty.user.id,
        status=DocumentStatus.FAILED,
        title="Scan",
    )
    message = "This PDF looks scanned: it has no selectable text, and <b>OCR</b> is not enabled."
    async with app.state.database.session() as db:
        row = await db.get(Document, doc.id)
        assert row is not None
        row.error = message
        await db.commit()
    html = (await faculty.client.get(f"/modules/{module.id}/materials/documents")).text
    assert 'class="doc-error"' in html
    assert "This PDF looks scanned: it has no selectable text" in html
    assert "<b>OCR</b>" not in html and "&lt;b&gt;OCR&lt;/b&gt;" in html  # escaped
    assert "passage" not in html
    api = (await faculty.client.get(f"/api/v1/documents/{doc.id}")).json()
    assert api["error"] == message and api["chunk_count"] is None


async def test_pending_documents_have_no_counts(
    app: FastAPI, seeded: None, make_actor: ActorFactory
) -> None:
    faculty = await make_actor(Role.FACULTY)
    module, _ = await _first_module(app)
    await insert_document(
        app, module_id=module.id, uploaded_by=faculty.user.id, status=DocumentStatus.PROCESSING
    )
    html = (await faculty.client.get(f"/modules/{module.id}/materials/documents")).text
    assert "Processing" in html and "passage" not in html
