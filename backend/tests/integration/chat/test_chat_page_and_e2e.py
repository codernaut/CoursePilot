"""The /chat page (per role, escaping, no inline scripts) and an upload -> worker -> chat round
trip whose citations resolve to the right passage."""

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from app import worker
from app.agent.runtime import AgentRuntime
from app.core.config import Settings
from app.models import Conversation, Document, Message
from app.models.enums import DocumentStatus, MessageRole, Role
from tests.conftest import Actor
from tests.docfiles import make_pptx, make_text_pdf
from tests.integration.chat.conftest import Llms, sse_of
from tests.integration.chat.test_chat_api import ask
from tests.integration.search.conftest import fake_embedder, module_ids

ActorFactory = Callable[..., Awaitable[Actor]]


# -- the page -----------------------------------------------------------------------------


@pytest.mark.parametrize("role", [Role.STUDENT, Role.FACULTY, Role.ADMIN])
async def test_chat_page_renders_for_every_role(
    make_actor: ActorFactory, corpus: dict[str, Any], role: Role
) -> None:
    actor = await make_actor(role)
    response = await actor.client.get("/chat")
    assert response.status_code == 200
    html = response.text
    assert 'id="chat-app"' in html and 'id="chat-form"' in html and 'id="chat-status"' in html
    assert 'aria-live="polite"' in html
    assert '<option value="">All material</option>' in html
    assert "Year 1 &rsaquo; Block 1" in html and "Module 1" in html
    assert 'href="/chat"' in html  # nav link
    assert "/static/js/chat.js" in html and "/static/js/passage_panel.js" in html
    assert 'id="passage-panel"' in html  # the shared citation panel
    assert "provider-alert" in html


async def test_chat_page_has_no_inline_scripts_styles_or_handlers(
    make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    actor = await make_actor(Role.STUDENT)
    done = dict(sse_of(await ask(actor)))["done"]
    for url in ("/chat", f"/chat/{done['conversation_id']}"):
        response = await actor.client.get(url)
        html = response.text
        assert not re.search(r"<script(?![^>]*\bsrc=)", html)
        assert not re.search(r"\sstyle=", html) and not re.search(r"\son\w+=", html)
        csp = response.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "unsafe-inline" not in csp
    js = await actor.client.get("/static/js/chat.js")
    assert js.status_code == 200
    assert "innerHTML" not in js.text and "eval(" not in js.text
    assert "document.write" not in js.text and "insertAdjacentHTML" not in js.text


async def test_chat_page_preselects_a_module_and_shows_the_existing_conversation(
    make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    actor = await make_actor(Role.STUDENT)
    mod = corpus["mods"]["1.1.2"]
    page = await actor.client.get("/chat", params={"module_id": mod})
    assert re.search(rf'<option value="{mod}" selected>', page.text)

    done = dict(sse_of(await ask(actor, module_id=corpus["mods"]["1.1.1"])))["done"]
    existing = await actor.client.get(f"/chat/{done['conversation_id']}")
    html = existing.text
    assert existing.status_code == 200
    assert "What does a binary heap keep at the root?" in html
    assert "Year 1 › Block 1 › Module 1" in html
    assert '<select id="chat-module" disabled>' in html  # a conversation keeps its module
    assert 'aria-current="page"' in html  # the current conversation in the sidebar
    assert "data-delete-conversation" in html


async def test_chat_page_of_someone_elses_conversation_is_404_and_not_listed(
    make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    owner = await make_actor(Role.STUDENT)
    done = dict(sse_of(await ask(owner, "a private question about heaps")))["done"]
    for role in (Role.STUDENT, Role.FACULTY, Role.ADMIN):
        other = await make_actor(role)
        assert (await other.client.get(f"/chat/{done['conversation_id']}")).status_code == 404
        assert "a private question" not in (await other.client.get("/chat")).text
    assert "a private question" in (await owner.client.get("/chat")).text


async def test_chat_page_escapes_messages_and_citation_data(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    owner = await make_actor(Role.STUDENT)
    evil = '<script>alert("x")</script> & <img src=x onerror=alert(1)>'
    async with app.state.database.session() as db:
        conversation = Conversation(
            user_id=owner.user.id, module_id=None, title=evil, thread_id="t" * 32
        )
        db.add(conversation)
        await db.flush()
        db.add(Message(conversation_id=conversation.id, role=MessageRole.USER, content=evil))
        db.add(
            Message(
                conversation_id=conversation.id,
                role=MessageRole.ASSISTANT,
                content=evil + " [C1]",
                citations=[
                    {
                        "marker": "C1",
                        "type": "material",
                        "chunk_id": "x'\"></div><script>alert(2)</script>",
                        "document_title": "<b>t</b>",
                    }
                ],
            )
        )
        await db.commit()
        cid = conversation.id
    html = (await owner.client.get(f"/chat/{cid}")).text
    assert "<script>alert" not in html and "<img src=x" not in html and "<b>t</b>" not in html
    assert "&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;" in html
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    sidebar = (await owner.client.get("/chat")).text
    assert "<script>alert" not in sidebar and "&lt;script&gt;" in sidebar


async def test_chat_page_without_a_provider_shows_short_or_admin_message(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any], settings: Settings
) -> None:
    settings.llm_provider = "anthropic"
    settings.anthropic_api_key = None
    app.state.agent_runtime = AgentRuntime(settings)
    student = await make_actor(Role.STUDENT)
    admin = await make_actor(Role.ADMIN)
    student_html = (await student.client.get("/chat")).text
    admin_html = (await admin.client.get("/chat")).text
    assert "AI provider not configured. Ask an administrator." in student_html
    assert "ANTHROPIC_API_KEY" not in student_html
    assert "AI provider not configured: the Anthropic chat provider needs ANTHROPIC_API_KEY" in (
        admin_html
    )
    assert "<textarea" in student_html and "disabled" in student_html
    assert 'id="chat-send" class="button" disabled' in student_html


async def test_chat_page_is_login_gated(client: AsyncClient) -> None:
    response = await client.get("/chat", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"


# -- upload -> worker -> chat -------------------------------------------------------------


async def wait_until(check: Callable[[], Awaitable[bool]], limit_s: float = 20.0) -> None:
    async with asyncio.timeout(limit_s):
        while not await check():  # noqa: ASYNC110 - polling the database
            await asyncio.sleep(0.05)


async def test_upload_worker_chat_citations_resolve_to_the_right_chunk_and_page(
    app: FastAPI,
    settings: Settings,
    seeded: None,
    make_actor: ActorFactory,
    llms: Llms,
) -> None:
    mods = await module_ids(app)
    faculty = await make_actor(Role.FACULTY)
    student = await make_actor(Role.STUDENT)
    filler = " ".join(f"Sorting note number {i} explains stable arrays." for i in range(1, 60))
    pdf = make_text_pdf(
        [
            filler,
            filler,
            "Zebrafish genome sequencing relies on shotgun assembly reads. " + filler,
        ]
    )
    pptx = make_pptx(
        [
            {"title": "Overview", "bullets": ["Welcome to the course. " * 40]},
            {"title": "Quokka habitat", "bullets": ["Quokkas live on Rottnest Island"] * 3},
        ]
    )
    for name, data in (("genomics.pdf", pdf), ("animals.pptx", pptx)):
        response = await faculty.client.post(
            f"/api/v1/modules/{mods['1.1.1']}/documents",
            files=[("file", (name, data, "application/octet-stream"))],
            headers=faculty.headers,
        )
        assert response.status_code == 202, response.text

    stop = asyncio.Event()
    task = asyncio.create_task(
        worker.run_worker(settings, stop, poll_interval_s=0.05, embedder_factory=fake_embedder)
    )
    try:

        async def all_ready() -> bool:
            async with app.state.database.read_session() as db:
                rows = (await db.execute(select(Document.status))).scalars().all()
            return len(rows) == 2 and all(s is DocumentStatus.READY for s in rows)

        await wait_until(all_ready)
    finally:
        stop.set()
        await asyncio.wait_for(task, 20)

    llms.main.responder = lambda m: "Zebrafish genomes are assembled from shotgun reads [C1]."
    response = await ask(
        student, "zebrafish genome shotgun assembly reads", module_id=mods["1.1.1"]
    )
    events = dict(sse_of(response))
    (cite,) = events["citations"]["citations"]
    assert cite["marker"] == "C1" and cite["document_title"] == "genomics"
    assert cite["page_end"] == 3 and cite["page_start"] in (2, 3)
    assert cite["location"].startswith("p") and cite["slide_number"] is None
    assert cite["path"] == "Year 1 › Block 1 › Module 1"

    # the cited chunk is the one the passage viewer opens, and the document link has the page
    passage = (await student.client.get(f"/api/v1/chunks/{cite['chunk_id']}")).json()
    assert "Zebrafish genome sequencing" in passage["text"]
    assert passage["document_id"] == cite["document_id"]
    assert passage["document_url"].endswith(f"/download#page={cite['page_start']}")

    # the same through the stored conversation
    cid = events["done"]["conversation_id"]
    detail = (await student.client.get(f"/api/v1/conversations/{cid}")).json()
    stored = detail["messages"][1]["citations"]
    assert stored == events["citations"]["citations"]

    # and a slide citation
    llms.main.responder = lambda m: "Quokkas live on Rottnest Island [C1]."
    slide = dict(sse_of(await ask(student, "quokkas rottnest island", module_id=mods["1.1.1"])))
    (slide_cite,) = slide["citations"]["citations"]
    assert slide_cite["document_title"] == "animals"
    assert slide_cite["slide_number"] == 2 and slide_cite["location"] == "slide 2"
