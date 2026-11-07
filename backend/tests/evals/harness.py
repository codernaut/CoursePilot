"""Retrieval eval harness: ingest the eval corpus into a temporary database with a given embedder,
run every question through the real search service and score the results.

Metrics (higher is better):
* hit@1 / hit@3 / hit@k: the share of questions whose expected source is among the top 1 / 3 / k
  retrieved chunks. A chunk is the expected source when it comes from the expected document and,
  if the question names a page or slide, that page lies inside the chunk's page range (or the
  slide is the chunk's first or last slide).
* MRR: the mean of 1 / rank of the first expected chunk (0 when it is not retrieved).
* facts: the share of questions whose first expected chunk contains every must-contain fact.

The harness is shared by the fast plumbing test (fake embedder) and the manual quality run (the
configured real embedder; see ``run.py``).
"""

import asyncio
import json
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.config import Settings
from app.db.chunk_store import recreate_vector_table, write_embedding_config
from app.db.migrate import upgrade_to_head
from app.db.session import Database
from app.ingestion import pipeline
from app.ingestion.embedder import Embedder
from app.ingestion.index_meta import config_from_settings
from app.ingestion.pipeline import PipelineDeps
from app.models import Block, Document, Module, User, Year
from app.models.enums import DocumentStatus, Role
from app.retrieval.access import AccessContext
from app.retrieval.hybrid import RetrievedChunk, SearchScope, best_similarity
from app.retrieval.query_embedding import QueryEmbedder
from app.services import search as search_service
from app.services import structure
from app.storage import LocalStorage, new_key
from tests.evals.corpus import DOCS, MODULES

QUESTIONS_FILE = Path(__file__).with_name("questions.json")
_SQUASH = " ".join


@dataclass(frozen=True)
class Question:
    id: str
    question: str
    document: str
    page: int | None
    slide: int | None
    facts: list[str]
    module: str | None = None


@dataclass
class QuestionResult:
    question: Question
    rank: int | None  # 1-based rank of the first expected chunk, None when absent
    facts_ok: bool
    retrieved: list[str] = field(default_factory=list)  # "Title (location)" per hit
    best_similarity: float | None = None  # best vector similarity of the hits (None: none)


@dataclass
class EvalReport:
    results: list[QuestionResult]
    k: int

    def _share(self, test: Callable[[QuestionResult], bool]) -> float:
        return sum(1 for r in self.results if test(r)) / len(self.results) if self.results else 0.0

    def hit_at(self, n: int) -> float:
        return self._share(lambda r: r.rank is not None and r.rank <= n)

    @property
    def mrr(self) -> float:
        if not self.results:
            return 0.0
        return sum(1.0 / r.rank for r in self.results if r.rank) / len(self.results)

    @property
    def facts(self) -> float:
        return self._share(lambda r: r.facts_ok)

    def similarity_distribution(self) -> str:
        """Best vector similarity per question, hits (expected source retrieved) vs misses, to
        help choose RETRIEVAL_MIN_SCORE: it should sit above most misses and below most hits."""

        def describe_group(name: str, values: list[float]) -> str:
            if not values:
                return f"  {name}: none"
            ordered = sorted(values)
            median = ordered[len(ordered) // 2]
            return (
                f"  {name} (n={len(ordered)}): min {ordered[0]:.3f}  median {median:.3f}  "
                f"max {ordered[-1]:.3f}"
            )

        hits: list[float] = []
        misses: list[float] = []
        for r in self.results:
            if r.best_similarity is not None:
                (hits if r.rank is not None else misses).append(r.best_similarity)
        return "\n".join(
            [
                "best vector similarity (RETRIEVAL_MIN_SCORE candidates):",
                describe_group("hits", hits),
                describe_group("misses", misses),
            ]
        )

    def summary(self) -> str:
        lines = [
            f"questions: {len(self.results)}",
            f"hit@1: {self.hit_at(1):.2f}   hit@3: {self.hit_at(3):.2f}   "
            f"hit@{self.k}: {self.hit_at(self.k):.2f}   MRR: {self.mrr:.3f}   "
            f"facts: {self.facts:.2f}",
        ]
        lines.append(self.similarity_distribution())
        for r in self.results:
            if r.rank != 1:
                got = "; ".join(r.retrieved[:3]) or "nothing"
                lines.append(
                    f"  - {r.question.id}: expected {r.question.document!r} "
                    f"rank={r.rank}, got: {got}"
                )
        return "\n".join(lines)


def load_questions() -> list[Question]:
    raw: list[dict[str, Any]] = json.loads(QUESTIONS_FILE.read_text(encoding="utf-8"))
    return [Question(**item) for item in raw]


def is_expected(chunk: RetrievedChunk, question: Question) -> bool:
    if chunk.document_title != question.document:
        return False
    if question.page is not None:
        start = chunk.page_start
        end = chunk.page_end if chunk.page_end is not None else start
        if start is None or end is None or not start <= question.page <= end:
            return False
    if question.slide is not None:
        last = chunk.metadata.get("slide_end", chunk.slide_number)
        first = chunk.slide_number
        if first is None or last is None or not first <= question.slide <= last:
            return False
    return True


def has_facts(chunk: RetrievedChunk, facts: list[str]) -> bool:
    text = _SQUASH(chunk.text.split()).casefold()
    return all(_SQUASH(fact.split()).casefold() in text for fact in facts)


def describe(chunk: RetrievedChunk) -> str:
    return f"{chunk.document_title} ({chunk.location or 'no page'})"


@dataclass
class EvalEnv:
    settings: Settings
    database: Database
    embedder: Embedder
    module_ids: dict[str, str]
    access: AccessContext

    async def close(self) -> None:
        await self.embedder.aclose()
        await self.database.dispose()


async def build_eval_env(
    settings: Settings, embedder: Embedder, *, only: set[str] | None = None
) -> EvalEnv:
    """Migrate a fresh database, seed the programme, ingest the corpus with ``embedder``."""
    await asyncio.to_thread(upgrade_to_head, settings)
    database = Database(settings.database_url)
    storage = LocalStorage(settings.data_dir)
    async with database.session() as db:
        await recreate_vector_table(db, settings.embedding_dim)
        await write_embedding_config(db, config_from_settings(settings))
        await structure.seed_structure(
            db,
            years=settings.program_years,
            blocks_per_year=settings.blocks_per_year,
            modules_per_block=settings.modules_per_block,
        )
        user = User(
            email="eval@example.edu",
            display_name="Eval",
            role=Role.FACULTY,
            password_hash="x",
            is_active=True,
            must_change_password=False,
        )
        db.add(user)
        module_ids: dict[str, str] = {}
        for key, (year, block, number) in MODULES.items():
            found = (
                await db.execute(
                    select(Module.id)
                    .join(Block, Module.block_id == Block.id)
                    .join(Year, Block.year_id == Year.id)
                    .where(Year.number == year, Block.number == block, Module.number == number)
                )
            ).scalar_one()
            module_ids[key] = str(found)
        await db.commit()
        access = AccessContext.from_user(user)

    deps = PipelineDeps(
        database=database, storage=storage, settings=settings, embedder_factory=lambda: embedder
    )
    for doc in DOCS:
        if only is not None and doc.title not in only:
            continue
        data = doc.build()
        key = new_key()

        async def body(payload: bytes = data) -> Any:
            yield payload

        await storage.put(key, body())
        async with database.session() as db:
            row = Document(
                module_id=module_ids[doc.module],
                uploaded_by=access.user_id,
                title=doc.title,
                original_filename=doc.filename,
                mime_type="application/octet-stream",
                size_bytes=len(data),
                storage_key=key,
                content_sha256=key * 2,
                status=DocumentStatus.UPLOADED,
                week=doc.week,
            )
            db.add(row)
            await db.commit()
            document_id = row.id
        await pipeline.process_document(deps, document_id)
    return EvalEnv(settings, database, embedder, module_ids, access)


async def run_eval(env: EvalEnv, questions: list[Question] | None = None) -> EvalReport:
    questions = questions if questions is not None else load_questions()
    queries = QueryEmbedder(env.settings, factory=lambda: env.embedder)
    k = env.settings.retrieval_top_k
    results: list[QuestionResult] = []
    for q in questions:
        scope = SearchScope("module", env.module_ids[q.module]) if q.module else SearchScope("all")
        async with env.database.read_session() as db:
            outcome = await search_service.search(
                db,
                settings=env.settings,
                embedder=queries,
                access=env.access,
                query=q.question,
                scope=scope,
            )
        chunks = outcome.chunks
        rank = next((i for i, c in enumerate(chunks, start=1) if is_expected(c, q)), None)
        facts_ok = rank is not None and has_facts(chunks[rank - 1], q.facts)
        results.append(
            QuestionResult(
                q, rank, facts_ok, [describe(c) for c in chunks], best_similarity(chunks)
            )
        )
    return EvalReport(results, k)


def temp_settings(base: Settings, folder: Path) -> Settings:
    """``base`` (the real configuration) pointed at a throwaway database and data directory."""
    return base.model_copy(
        update={
            "data_dir": str(folder),
            "database_url": f"sqlite+aiosqlite:///{folder / 'eval.db'}",
            "checkpoint_db_path": str(folder / "checkpoints.db"),
        }
    )


async def evaluate(base: Settings, embedder: Embedder) -> EvalReport:
    with tempfile.TemporaryDirectory(prefix="coursepilot-eval-") as folder:
        env = await build_eval_env(temp_settings(base, Path(folder)), embedder)
        try:
            return await run_eval(env)
        finally:
            await env.close()
