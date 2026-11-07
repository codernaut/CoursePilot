"""End-to-end smoke test (CLAUDE.md section 12): one test drives the real app over HTTP with the
fake embedder, fake LLM and fake web search.

create admin (CLI function) -> login -> create faculty and student -> faculty uploads a PDF, a PPTX
and a Markdown file -> worker processes the jobs -> student searches and chats (module, widened to
the block, all material, web fallback) -> citations resolve -> feedback -> admin sees jobs, usage
and feedback -> student deletes a conversation -> faculty deletes a document -> search no longer
returns it."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, select

from app import cli, worker
from app.core.config import Settings
from app.ingestion.embedder import FakeEmbedder
from app.main import create_app
from app.models import Chunk, Document, Message
from app.models.enums import DocumentStatus
from app.websearch.fake import FakeWebSearch
from tests.conftest import api_login
from tests.docfiles import make_pptx, make_text_pdf
from tests.ingest_support import DIM, MODEL
from tests.integration.chat.conftest import Llms, sse_of
from tests.integration.search.conftest import fake_embedder, module_ids

ADMIN_EMAIL, ADMIN_PASSWORD = "root@example.edu", "Admin-Pass-Phrase-7"
NEW_PASSWORD = "Fresh-Pass-Phrase-9"


@dataclass
class Session:
    client: AsyncClient
    csrf: str

    @property
    def headers(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.csrf}


async def ask(session: Session, message: str, **body: Any) -> Response:
    return await session.client.post(
        "/api/v1/chat", json={"message": message, **body}, headers=session.headers
    )


@pytest.fixture
def web() -> FakeWebSearch:
    return FakeWebSearch()


@pytest.fixture
async def app(settings: Settings, llms: Llms, web: FakeWebSearch) -> AsyncIterator[FastAPI]:
    application = create_app(
        settings,
        embedder_factory=lambda: FakeEmbedder(model=MODEL, dim=DIM),
        llm_factory=llms.models,
        web_search_factory=lambda _settings: web,
    )
    yield application
    await application.state.database.dispose()


async def run_worker_until_idle(settings: Settings, app: FastAPI, *, expect_jobs: int) -> None:
    """Run the real worker loop until every job has finished (done or failed)."""
    from app.models import Job
    from app.models.enums import JobStatus

    stop = asyncio.Event()
    task = asyncio.create_task(
        worker.run_worker(settings, stop, poll_interval_s=0.05, embedder_factory=fake_embedder)
    )
    try:
        async with asyncio.timeout(60):
            while True:
                async with app.state.database.read_session() as db:
                    statuses = list((await db.execute(select(Job.status))).scalars())
                if len(statuses) >= expect_jobs and all(
                    s in (JobStatus.DONE, JobStatus.FAILED) for s in statuses
                ):
                    return
                await asyncio.sleep(0.05)
    finally:
        stop.set()
        await asyncio.wait_for(task, 30)


async def test_the_whole_product_over_http(
    app: FastAPI,
    settings: Settings,
    llms: Llms,
    web: FakeWebSearch,
) -> None:
    stack = AsyncExitStack()

    async def login(email: str, password: str) -> Session:
        http = await stack.enter_async_context(
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                headers={"Origin": "http://test"},
            )
        )
        return Session(http, await api_login(http, email, password))

    try:
        # -- an administrator through the CLI functions, then everyone else through the API ----
        await asyncio.to_thread(cli.run_seed, settings)
        await asyncio.to_thread(cli._create_admin, settings, ADMIN_EMAIL, "Root", ADMIN_PASSWORD)
        admin = await login(ADMIN_EMAIL, ADMIN_PASSWORD)

        people: dict[str, Session] = {}
        for role, email in (("faculty", "prof@example.edu"), ("student", "sam@example.edu")):
            created = await admin.client.post(
                "/api/v1/users",
                json={"email": email, "display_name": role.title(), "role": role},
                headers=admin.headers,
            )
            assert created.status_code == 201, created.text
            temporary = created.json()["temporary_password"]
            session = await login(email, temporary)
            # the first login only allows changing the password
            gated = await session.client.get("/api/v1/structure")
            assert gated.status_code == 403
            assert gated.json()["error"]["code"] == "password_change_required"
            changed = await session.client.post(
                "/api/v1/auth/change-password",
                json={"current_password": temporary, "new_password": NEW_PASSWORD},
                headers=session.headers,
            )
            assert changed.status_code == 200, changed.text
            assert (await session.client.get("/api/v1/structure")).status_code == 200
            people[role] = session
        faculty, student = people["faculty"], people["student"]

        mods = await module_ids(app)
        module = mods["1.1.1"]

        # -- uploads: students cannot, faculty can ------------------------------------------------
        filler = " ".join(f"Sorting note number {i} explains stable arrays." for i in range(1, 60))
        pdf = make_text_pdf(
            [filler, filler, "Zebrafish genome sequencing relies on shotgun assembly. " + filler]
        )
        pptx = make_pptx(
            [
                {"title": "Overview", "bullets": ["Welcome to the course. " * 40]},
                {"title": "Quokka habitat", "bullets": ["Quokkas live on Rottnest Island"] * 3},
            ]
        )
        graphs = b"# Graphs\n\nDijkstra shortest path algorithm uses a priority queue.\n"
        refused = await student.client.post(
            f"/api/v1/modules/{module}/documents",
            files=[("file", ("genomics.pdf", pdf, "application/pdf"))],
            headers=student.headers,
        )
        assert refused.status_code == 403
        uploaded: dict[str, str] = {}
        for target, name, data in (
            (module, "genomics.pdf", pdf),
            (module, "animals.pptx", pptx),
            (mods["1.1.2"], "graphs.md", graphs),
        ):
            response = await faculty.client.post(
                f"/api/v1/modules/{target}/documents",
                files=[("file", (name, data, "application/octet-stream"))],
                headers=faculty.headers,
            )
            assert response.status_code == 202, response.text
            uploaded[name] = response.json()["documents"][0]["id"]

        # nothing is searchable before the worker has run
        before = (await student.client.get("/api/v1/search", params={"q": "zebrafish"})).json()
        assert before["hits"] == []

        await run_worker_until_idle(settings, app, expect_jobs=3)
        listed = (await student.client.get(f"/api/v1/modules/{module}/documents")).json()
        assert {d["original_filename"]: d["status"] for d in listed} == {
            "genomics.pdf": "ready",
            "animals.pptx": "ready",
        }

        # -- search ------------------------------------------------------------------------------
        found = (
            await student.client.get("/api/v1/search", params={"q": "zebrafish genome"})
        ).json()
        assert found["hits"][0]["document_title"] == "genomics"

        # -- chat: the module itself --------------------------------------------------------------
        llms.main.responder = lambda m: "Zebrafish genomes are assembled from shotgun reads [C1]."
        events = dict(
            sse_of(await ask(student, "zebrafish genome shotgun assembly", module_id=module))
        )
        assert events["done"]["searched"] == ["module"] and events["done"]["used_web"] is False
        (cite,) = events["citations"]["citations"]
        assert cite["marker"] == "C1" and cite["document_title"] == "genomics"
        assert cite["page_end"] == 3 and cite["path"] == "Year 1 › Block 1 › Module 1"
        passage = (await student.client.get(f"/api/v1/chunks/{cite['chunk_id']}")).json()
        assert "Zebrafish genome sequencing" in passage["text"]
        assert passage["document_url"].endswith(f"/download#page={cite['page_start']}")
        download = await student.client.get(f"/api/v1/documents/{cite['document_id']}/download")
        assert download.status_code == 200 and download.content == pdf
        first_conversation = events["done"]["conversation_id"]
        first_message = events["done"]["message_id"]

        # -- chat: an empty module widens to its block --------------------------------------------
        llms.main.responder = lambda m: "Dijkstra uses a priority queue [C1]."
        widened = dict(
            sse_of(
                await ask(student, "dijkstra shortest path priority queue", module_id=mods["1.1.3"])
            )
        )
        assert widened["done"]["searched"] == ["module", "block"]
        assert widened["citations"]["citations"][0]["document_title"] == "graphs"

        # -- chat: all material -------------------------------------------------------------------
        llms.main.responder = lambda m: "Quokkas live on Rottnest Island [C1]."
        everything = dict(sse_of(await ask(student, "quokkas rottnest island")))
        assert everything["done"]["searched"] == ["all"]
        assert everything["citations"]["citations"][0]["slide_number"] == 2
        assert web.queries == []  # material was enough every time so far

        # -- chat: nothing relevant anywhere -> the web, clearly marked ---------------------------
        settings.retrieval_min_score = 0.99
        llms.main.responder = lambda m: "From the web: kangaroos are marsupials [W1]."
        webbed = dict(sse_of(await ask(student, "what is a kangaroo", module_id=module)))
        settings.retrieval_min_score = 0.0
        assert web.queries == ["what is a kangaroo"]
        assert webbed["done"]["used_web"] is True and webbed["done"]["searched"] == [
            "module",
            "block",
            "all",
        ]
        (web_cite,) = webbed["citations"]["citations"]
        assert web_cite["kind"] == "web" and web_cite["url"] == "https://example.org/page-1"

        # -- feedback and conversation privacy -----------------------------------------------------
        rated = await student.client.post(
            f"/api/v1/messages/{first_message}/feedback",
            json={"feedback": "down", "comment": "Needs the page number."},
            headers=student.headers,
        )
        assert rated.status_code == 200 and rated.json()["feedback"] == "down"
        conversations = (await student.client.get("/api/v1/conversations")).json()
        assert len(conversations) == 4
        for outsider in (admin, faculty):
            peek = await outsider.client.get(f"/api/v1/conversations/{first_conversation}")
            assert peek.status_code == 404

        # -- the administrator's view ------------------------------------------------------------
        jobs = (await admin.client.get("/api/v1/admin/jobs")).json()
        assert jobs["total"] == 3
        assert {j["status"] for j in jobs["items"]} == {"done"}
        assert {j["document_title"] for j in jobs["items"]} == {"genomics", "animals", "graphs"}
        usage = (await admin.client.get("/api/v1/admin/usage")).json()
        assert usage["top_users"][0]["email"] == "sam@example.edu"
        assert usage["top_users"][0]["messages_today"] == 4
        assert usage["messages_per_day"][-1]["questions"] == 4
        assert usage["messages_per_day"][-1]["answers"] == 4
        assert usage["web_answers"] == 1 and usage["documents_by_status"] == {"ready": 3}
        assert usage["tokens_per_day"] and usage["tokens_per_day"][0]["total_tokens"] > 0
        assert usage["storage_bytes"] == len(pdf) + len(pptx) + len(graphs)
        feedback = (await admin.client.get("/api/v1/admin/feedback")).json()
        assert [(f["comment"], f["question"]) for f in feedback] == [
            ("Needs the page number.", "zebrafish genome shotgun assembly")
        ]
        assert "sam@example.edu" not in str(feedback)
        for page in ("/admin/jobs", "/admin/usage"):
            assert (await admin.client.get(page)).status_code == 200
            assert (await student.client.get(page)).status_code == 403

        # -- the student deletes a conversation ---------------------------------------------------
        deleted = await student.client.delete(
            f"/api/v1/conversations/{first_conversation}", headers=student.headers
        )
        assert deleted.status_code == 204
        assert (
            await student.client.get(f"/api/v1/conversations/{first_conversation}")
        ).status_code == 404
        assert (await admin.client.get("/api/v1/admin/feedback")).json() == []
        async with app.state.database.read_session() as db:
            gone = await db.get(Message, first_message)
            assert gone is None

        # -- faculty deletes a document: hidden at once, removed by the worker --------------------
        pdf_id = uploaded["genomics.pdf"]
        async with app.state.database.read_session() as db:
            chunk_total = (
                await db.execute(
                    select(func.count()).select_from(Chunk).where(Chunk.document_id == pdf_id)
                )
            ).scalar_one()
            pdf_key = (await db.get(Document, pdf_id)).storage_key
        assert chunk_total > 0 and await app.state.storage.exists(pdf_key)
        removed = await faculty.client.delete(
            f"/api/v1/documents/{pdf_id}", headers=faculty.headers
        )
        assert removed.status_code == 202 and removed.json()["status"] == "deleting"
        hidden = (
            await student.client.get("/api/v1/search", params={"q": "zebrafish genome"})
        ).json()
        assert all(hit["document_id"] != pdf_id for hit in hidden["hits"])
        assert (await student.client.get(f"/api/v1/documents/{pdf_id}")).status_code == 404
        await run_worker_until_idle(settings, app, expect_jobs=4)
        async with app.state.database.read_session() as db:
            assert await db.get(Document, pdf_id) is None
            leftover = (
                await db.execute(
                    select(func.count()).select_from(Chunk).where(Chunk.document_id == pdf_id)
                )
            ).scalar_one()
            remaining = (await db.execute(select(Document.status))).scalars().all()
        assert leftover == 0 and set(remaining) == {DocumentStatus.READY}
        assert (await student.client.get(f"/api/v1/chunks/{cite['chunk_id']}")).status_code == 404
        after = (
            await student.client.get("/api/v1/search", params={"q": "zebrafish genome"})
        ).json()
        assert all(hit["document_id"] != pdf_id for hit in after["hits"])
        assert not await app.state.storage.exists(pdf_key)  # the stored file is gone too
    finally:
        await stack.aclose()
