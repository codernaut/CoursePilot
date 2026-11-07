"""Helpers for retrieval tests: a small four-module corpus ingested with the fake embedder."""

from dataclasses import dataclass

from sqlalchemy import select, text

from app.ingestion import pipeline
from app.models import Block, Document, Module, User, Year
from app.models.enums import DocumentStatus
from app.retrieval.access import AccessContext
from tests.ingest_support import Env

HEAPS = (
    b"# Heaps\n\nA binary heap keeps the largest item at the root of the tree. "
    b"Heap sort repeatedly extracts the root of the heap.\n"
)
GRAPHS = (
    b"# Graphs\n\nDijkstra shortest path algorithm uses a priority queue over weighted "
    b"graph edges.\n"
)
PLANTS = (
    b"# Photosynthesis\n\nChloroplasts convert sunlight, water and carbon dioxide into "
    b"glucose and oxygen.\n"
)
LEDGERS = (
    b"# Ledgers\n\nDouble entry bookkeeping records every transaction as a debit and a credit.\n"
)


@dataclass
class Corpus:
    env: Env
    same_module: str  # Year 1 / Block 1 / Module 1 (holds HEAPS)
    same_block: str  # Year 1 / Block 1 / Module 2 (holds GRAPHS)
    other_block: str  # Year 1 / Block 2 / Module 1 (holds PLANTS)
    other_year: str  # Year 2 / Block 1 / Module 1 (holds LEDGERS)
    block_id: str  # the block of same_module
    docs: dict[str, str]  # title -> document id
    access: AccessContext


async def module_id(env: Env, year: int, block: int, number: int) -> str:
    async with env.database.read_session() as db:
        found = (
            await db.execute(
                select(Module.id)
                .join(Block, Module.block_id == Block.id)
                .join(Year, Block.year_id == Year.id)
                .where(Year.number == year, Block.number == block, Module.number == number)
            )
        ).scalar_one()
        return str(found)


async def ingest(
    env: Env,
    data: bytes,
    *,
    title: str,
    module: str | None = None,
    week: str | None = None,
    filename: str | None = None,
) -> Document:
    """Add a document and run the real pipeline on it (it ends ``ready``)."""
    doc = await env.add_document(
        data, filename or f"{title.lower().replace(' ', '-')}.md", title=title, module_id=module,
        week=week,
    )  # fmt: skip
    await pipeline.process_document(env.deps, doc.id)
    return doc


async def set_status(env: Env, document_id: str, status: DocumentStatus) -> None:
    async with env.database.session() as db:
        document = await db.get(Document, document_id)
        assert document is not None
        document.status = status
        await db.commit()


async def access_for_faculty(env: Env) -> AccessContext:
    async with env.database.read_session() as db:
        user = (await db.execute(select(User))).scalars().first()
        assert user is not None
        return AccessContext.from_user(user)


async def build_corpus(env: Env) -> Corpus:
    m1 = env.module_id
    m2 = await module_id(env, 1, 1, 2)
    m3 = await module_id(env, 1, 2, 1)
    m4 = await module_id(env, 2, 1, 1)
    docs: dict[str, str] = {}
    for title, data, module, week in (
        ("Heaps", HEAPS, m1, "Week 1"),
        ("Graphs", GRAPHS, m2, "Week 2"),
        ("Plants", PLANTS, m3, "Week 1"),
        ("Ledgers", LEDGERS, m4, None),
    ):
        docs[title] = (await ingest(env, data, title=title, module=module, week=week)).id
    async with env.database.read_session() as db:
        block_id = str(
            (await db.execute(select(Module.block_id).where(Module.id == m1))).scalar_one()
        )
    return Corpus(env, m1, m2, m3, m4, block_id, docs, await access_for_faculty(env))


async def fts_row_count(env: Env) -> int:
    async with env.database.read_session() as db:
        return int((await db.execute(text("SELECT count(*) FROM chunks_fts"))).scalar_one())
