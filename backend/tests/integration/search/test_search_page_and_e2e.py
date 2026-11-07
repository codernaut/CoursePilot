"""The /search page (rendering, escaping, panel partial) and an upload -> worker -> search round
trip with page and slide numbers."""

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select, text

from app import worker
from app.core.config import Settings
from app.models import Document
from app.models.enums import DocumentStatus, Role
from app.retrieval.hybrid import PATH_SEPARATOR
from tests.conftest import Actor
from tests.docfiles import make_pptx, make_text_pdf
from tests.integration.search.conftest import fake_embedder, ingest_markdown, module_ids

ActorFactory = Callable[..., Awaitable[Actor]]

EVIL_TITLE = "<script>alert(1)</script> Heaps"
EVIL_BODY = (
    '# Heaps\n\nA binary heap "quoted" & tagged `<b>bold</b>` keeps the largest item at the root.\n'
)


@pytest.fixture
async def corpus(app: FastAPI, settings: Settings, seeded: None) -> dict[str, Any]:
    mods = await module_ids(app)
    doc = await ingest_markdown(
        app, settings, module_id=mods["1.1.1"], title=EVIL_TITLE, body=EVIL_BODY
    )
    return {"mods": mods, "doc": doc}


@pytest.mark.parametrize("role", [Role.STUDENT, Role.FACULTY, Role.ADMIN])
async def test_search_page_renders_for_every_role(
    make_actor: ActorFactory, corpus: dict[str, Any], role: Role
) -> None:
    actor = await make_actor(role)
    response = await actor.client.get("/search")
    assert response.status_code == 200
    html = response.text
    assert "<h1>Search the material</h1>" in html
    assert '<label for="search-q">' in html and 'name="q"' in html
    assert 'href="/search"' in html  # nav link
    assert 'id="passage-panel"' in html and 'role="dialog"' in html and 'aria-modal="true"' in html
    assert 'id="passage-close"' in html and 'aria-live="polite"' in html
    assert '<script src="/static/js/passage_panel.js" defer></script>' in html
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)  # no inline scripts
    assert "No matching passages" not in html  # nothing searched yet


async def test_search_page_shows_results_escaped(
    make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    actor = await make_actor(Role.STUDENT)
    response = await actor.client.get("/search", params={"q": "binary heap root"})
    assert response.status_code == 200
    html = response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt; Heaps" in html
    assert "<script>alert(1)" not in html
    assert "<b>bold</b>" not in html and "&lt;b&gt;bold&lt;/b&gt;" in html
    assert "&amp; tagged" in html
    assert 'data-passage-id="' in html and "View passage" in html
    assert PATH_SEPARATOR.join(["Year 1", "Block 1", "Module 1"]) in html
    # the query is echoed escaped too
    response = await actor.client.get("/search", params={"q": '"><script>alert(2)</script>'})
    assert "<script>alert(2)" not in response.text
    assert "&gt;&lt;script&gt;alert(2)" in response.text


async def test_search_page_scope_and_empty_results(
    make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    actor = await make_actor(Role.STUDENT)
    mods = corpus["mods"]
    empty = await actor.client.get("/search", params={"q": "heap", "module_id": mods["1.3.1"]})
    assert empty.status_code == 200 and "No matching passages were found." in empty.text
    widened = await actor.client.get(
        "/search", params={"q": "heap", "module_id": mods["1.3.1"], "widen": "1"}
    )
    assert "widening the search" in widened.text and "View passage" in widened.text
    missing = await actor.client.get("/search", params={"q": "heap", "module_id": "0" * 32})
    assert missing.status_code == 404 and "Module not found" in missing.text
    bad = await actor.client.get("/search", params={"q": "heap", "scope": "block"})
    assert bad.status_code == 422 and 'role="alert"' in bad.text


async def test_search_page_reports_index_mismatch(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    async with app.state.database.session() as db:
        await db.execute(text("UPDATE app_meta SET value = '99' WHERE key = 'embedding_dim'"))
        await db.commit()
    actor = await make_actor(Role.STUDENT)
    response = await actor.client.get("/search", params={"q": "heap"})
    assert response.status_code == 503
    assert "reindex-all" in response.text and 'role="alert"' in response.text


async def test_anonymous_search_page_redirects_to_login(client: AsyncClient) -> None:
    response = await client.get("/search?q=heap", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"


async def test_panel_script_is_static_and_uses_text_content_only(client: AsyncClient) -> None:
    response = await client.get("/static/js/passage_panel.js")
    assert response.status_code == 200
    source = response.text
    assert "Escape" in source and "textContent" in source and "Tab" in source
    assert "innerHTML" not in source and "insertAdjacentHTML" not in source
    assert "eval(" not in source


async def test_pages_set_csp_without_inline_script_allowance(
    make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    actor = await make_actor(Role.STUDENT)
    response = await actor.client.get("/search")
    csp = response.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp


# --- upload -> worker -> search ------------------------------------------------------------------


async def wait_until(check: Callable[[], Awaitable[bool]], limit_s: float = 20.0) -> None:
    async with asyncio.timeout(limit_s):
        while not await check():  # noqa: ASYNC110 - polling the database
            await asyncio.sleep(0.05)


async def test_upload_then_worker_then_search_gives_correct_pages_and_slides(
    app: FastAPI, settings: Settings, seeded: None, make_actor: ActorFactory
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

    response = await student.client.get(
        "/api/v1/search", params={"q": "zebrafish genome shotgun assembly"}
    )
    assert response.status_code == 200
    top = response.json()["hits"][0]
    assert top["document_title"] == "genomics"
    assert top["page_end"] == 3 and top["page_start"] in (2, 3)  # the page the passage is on
    assert top["location"].startswith("p")
    assert top["slide_number"] is None

    response = await student.client.get(
        "/api/v1/search", params={"q": "quokkas rottnest island", "module_id": mods["1.1.1"]}
    )
    top = response.json()["hits"][0]
    assert top["document_title"] == "animals"
    assert top["slide_number"] == 2 and top["location"] == "slide 2"

    passage = (await student.client.get(f"/api/v1/chunks/{top['chunk_id']}")).json()
    assert "Quokkas live on Rottnest Island" in passage["text"]
    assert passage["document_url"].endswith("/download")  # slides have no #page anchor

    page_hit = (
        await student.client.get("/api/v1/search", params={"q": "zebrafish genome"})
    ).json()["hits"][0]
    page_passage = (await student.client.get(f"/api/v1/chunks/{page_hit['chunk_id']}")).json()
    assert page_passage["document_url"].endswith(f"/download#page={page_hit['page_start']}")
