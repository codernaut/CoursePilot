"""The materials pages: picker, per-module page, HTMX list fragment and the faculty forms."""

import re
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select, update

from app.models import Document, Job, Module, User
from app.models.enums import DocumentStatus, JobType, Role
from tests.conftest import Actor, insert_document

ActorFactory = Callable[..., Awaitable[Actor]]
NOTICE = "All users of this site can see uploaded material."


@pytest.fixture
async def module(app: FastAPI, seeded: None) -> Module:
    async with app.state.database.read_session() as db:
        found = (await db.execute(select(Module).order_by(Module.number))).scalars().first()
    assert isinstance(found, Module)
    return found


def inline_scripts(html: str) -> list[str]:
    return [m for m in re.findall(r"<script\b[^>]*>", html) if "src=" not in m]


def event_handlers(html: str) -> list[str]:
    return re.findall(r"\son[a-z]+\s*=", html, flags=re.IGNORECASE)


async def page(actor: Actor, module: Module) -> str:
    response = await actor.client.get(f"/modules/{module.id}/materials")
    assert response.status_code == 200
    return response.text


# --- picker ----------------------------------------------------------------------------------


async def test_picker_lists_every_module_with_links(
    make_actor: ActorFactory, module: Module
) -> None:
    student = await make_actor(Role.STUDENT)
    response = await student.client.get("/materials")
    assert response.status_code == 200
    html = response.text
    assert html.count('<a href="/modules/') == 27  # 3 x 3 x 3 modules
    assert f'href="/modules/{module.id}/materials"' in html
    assert '<label for="module-picker">Module</label>' in html
    assert "<optgroup" in html and "Year 1" in html and "Block 1" in html


async def test_picker_form_redirects_to_the_module(
    make_actor: ActorFactory, module: Module
) -> None:
    student = await make_actor(Role.STUDENT)
    response = await student.client.get(
        "/materials", params={"module_id": module.id}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/modules/{module.id}/materials"
    missing = await student.client.get("/materials", params={"module_id": "0" * 32})
    assert missing.status_code == 404


async def test_nav_and_home_link_to_materials(make_actor: ActorFactory, module: Module) -> None:
    student = await make_actor(Role.STUDENT)
    home = (await student.client.get("/")).text
    assert '<a href="/materials"' in home
    assert f'href="/modules/{module.id}/materials"' in home


# --- the module page, per role ---------------------------------------------------------------


async def test_faculty_sees_upload_area_notice_and_controls(
    app: FastAPI, make_actor: ActorFactory, module: Module
) -> None:
    faculty = await make_actor(Role.FACULTY)
    await insert_document(
        app, module_id=module.id, uploaded_by=faculty.user.id, title="Lecture 1", filename="l1.pdf"
    )
    html = await page(faculty, module)
    assert 'id="drop-zone"' in html
    assert f'data-upload-url="/api/v1/modules/{module.id}/documents"' in html
    assert f'data-max-bytes="{50 * 1024 * 1024}"' in html
    assert 'data-extensions="pdf,pptx,docx,md,txt,html"' in html
    assert '<label for="file-input">' in html
    assert re.search(r'<input id="file-input" type="file" multiple accept="\.pdf,', html)
    assert NOTICE in html
    assert "right to share" in html
    # The notice comes before the file chooser.
    assert html.index(NOTICE) < html.index('id="file-input"')
    assert 'id="upload-status"' in html and 'aria-live="polite"' in html
    assert 'id="upload-queue"' in html
    assert "/delete" in html and "/reindex" in html
    assert 'data-confirm="Delete &quot;Lecture 1&quot;' in html
    assert 'name="csrf_token"' in html
    assert "Download" in html or "Open" in html
    assert 'href="/api/v1/documents/' in html and "/download" in html


@pytest.mark.parametrize("role", [Role.STUDENT, Role.ADMIN])
async def test_students_and_admins_get_the_list_without_controls(
    app: FastAPI, make_actor: ActorFactory, module: Module, role: Role
) -> None:
    uploader = await make_actor(Role.FACULTY)
    doc = await insert_document(
        app, module_id=module.id, uploaded_by=uploader.user.id, title="Lecture 1"
    )
    viewer = await make_actor(role)
    html = await page(viewer, module)
    assert "Lecture 1" in html
    assert f'href="/api/v1/documents/{doc.id}/download"' in html
    assert 'id="drop-zone"' not in html
    assert 'id="file-input"' not in html
    assert "/delete" not in html and "/reindex" not in html
    assert "only faculty upload material" in html.lower()
    assert NOTICE in html


async def test_empty_module_page(make_actor: ActorFactory, module: Module) -> None:
    html = await page(await make_actor(Role.STUDENT), module)
    assert "No material has been uploaded to this module yet." in html


async def test_unknown_module_page_is_404(make_actor: ActorFactory, seeded: None) -> None:
    student = await make_actor(Role.STUDENT)
    assert (await student.client.get(f"/modules/{'0' * 32}/materials")).status_code == 404
    assert (await student.client.get(f"/modules/{'0' * 32}/materials/documents")).status_code == 404


@pytest.mark.parametrize("role", [Role.STUDENT, Role.FACULTY, Role.ADMIN])
async def test_pages_have_no_inline_scripts_or_handlers(
    app: FastAPI, make_actor: ActorFactory, module: Module, role: Role
) -> None:
    actor = await make_actor(role)
    await insert_document(app, module_id=module.id, uploaded_by=actor.user.id)
    for path in ("/materials", f"/modules/{module.id}/materials"):
        html = (await actor.client.get(path)).text
        assert inline_scripts(html) == [], path
        assert event_handlers(html) == [], path
        assert "javascript:" not in html
    page_html = await page(actor, module)
    assert '<script src="/static/js/materials.js" defer></script>' in page_html
    csp = (await actor.client.get(f"/modules/{module.id}/materials")).headers[
        "content-security-policy"
    ]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp


async def test_static_script_is_served_and_sends_the_csrf_header(client: AsyncClient) -> None:
    response = await client.get("/static/js/materials.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert "X-CSRF-Token" in response.text and "XMLHttpRequest" in response.text
    assert "eval(" not in response.text and "innerHTML" not in response.text


async def test_document_text_is_escaped(
    app: FastAPI, make_actor: ActorFactory, module: Module
) -> None:
    faculty = await make_actor(Role.FACULTY)
    await insert_document(
        app,
        module_id=module.id,
        uploaded_by=faculty.user.id,
        title='<b>bold</b> "quoted" & <script>alert(1)</script>',
        filename="<img src=x onerror=alert(1)>.pdf",
    )
    for html in (
        await page(faculty, module),
        (await faculty.client.get(f"/modules/{module.id}/materials/documents")).text,
    ):
        assert "<b>bold</b>" not in html
        assert "<script>alert(1)</script>" not in html
        assert "<img src=x" not in html
        assert "&lt;b&gt;bold&lt;/b&gt;" in html
        assert "&lt;img src=x onerror=alert(1)&gt;.pdf" in html
        assert inline_scripts(html) == []


async def test_failed_document_shows_its_error_escaped(
    app: FastAPI, make_actor: ActorFactory, module: Module
) -> None:
    faculty = await make_actor(Role.FACULTY)
    doc = await insert_document(
        app, module_id=module.id, uploaded_by=faculty.user.id, status=DocumentStatus.FAILED
    )
    async with app.state.database.session() as db:
        await db.execute(
            update(Document)
            .where(Document.id == doc.id)
            .values(error="Could not read page 3 <i>x</i>")
        )
        await db.commit()
    html = (await faculty.client.get(f"/modules/{module.id}/materials/documents")).text
    assert "Could not read page 3 &lt;i&gt;x&lt;/i&gt;" in html
    assert "Failed" in html
    assert "/reindex" in html  # failed documents can be re-indexed


# --- the HTMX list fragment ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "polls"),
    [
        (DocumentStatus.UPLOADED, True),
        (DocumentStatus.PROCESSING, True),
        (DocumentStatus.READY, False),
        (DocumentStatus.FAILED, False),
    ],
)
async def test_fragment_polls_only_while_work_is_pending(
    app: FastAPI,
    make_actor: ActorFactory,
    module: Module,
    status: DocumentStatus,
    polls: bool,
) -> None:
    student = await make_actor(Role.STUDENT)
    uploader = await make_actor(Role.FACULTY)
    await insert_document(app, module_id=module.id, uploaded_by=uploader.user.id, status=status)
    html = (await student.client.get(f"/modules/{module.id}/materials/documents")).text
    assert html.lstrip().startswith('<div id="document-list"')
    assert ('hx-trigger="every 3s"' in html) is polls
    if polls:
        assert f'hx-get="/modules/{module.id}/materials/documents"' in html
        assert 'hx-swap="outerHTML"' in html
    # The same markup is embedded in the full page.
    assert ('hx-trigger="every 3s"' in await page(student, module)) is polls


async def test_one_pending_document_keeps_the_whole_list_polling(
    app: FastAPI, make_actor: ActorFactory, module: Module
) -> None:
    faculty = await make_actor(Role.FACULTY)
    await insert_document(
        app, module_id=module.id, uploaded_by=faculty.user.id, content=b"%PDF-1 a"
    )
    await insert_document(
        app,
        module_id=module.id,
        uploaded_by=faculty.user.id,
        content=b"%PDF-1 b",
        status=DocumentStatus.PROCESSING,
    )
    html = (await faculty.client.get(f"/modules/{module.id}/materials/documents")).text
    assert 'hx-trigger="every 3s"' in html
    assert 'data-status="ready"' in html and 'data-status="processing"' in html
    assert "Processing" in html and "Ready" in html


async def test_fragment_hides_deleting_documents_and_other_modules(
    app: FastAPI, make_actor: ActorFactory, module: Module
) -> None:
    faculty = await make_actor(Role.FACULTY)
    async with app.state.database.read_session() as db:
        others = (await db.execute(select(Module).order_by(Module.number))).scalars().all()
    await insert_document(
        app,
        module_id=module.id,
        uploaded_by=faculty.user.id,
        title="Hidden",
        content=b"%PDF-1 h",
        status=DocumentStatus.DELETING,
    )
    await insert_document(
        app,
        module_id=others[1].id,
        uploaded_by=faculty.user.id,
        title="Elsewhere",
        content=b"%PDF-1 e",
    )
    html = (await faculty.client.get(f"/modules/{module.id}/materials/documents")).text
    assert "Hidden" not in html and "Elsewhere" not in html
    assert "No material has been uploaded" in html


# --- delete / re-index forms -----------------------------------------------------------------


async def test_delete_form_hides_the_document_and_enqueues_the_job(
    app: FastAPI, make_actor: ActorFactory, module: Module
) -> None:
    faculty = await make_actor(Role.FACULTY)
    doc = await insert_document(app, module_id=module.id, uploaded_by=faculty.user.id)
    response = await faculty.client.post(
        f"/modules/{module.id}/materials/{doc.id}/delete",
        data={"csrf_token": faculty.csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/modules/{module.id}/materials?notice=deleting"
    async with app.state.database.read_session() as db:
        document = await db.get(Document, doc.id)
        jobs = (await db.execute(select(Job))).scalars().all()
    assert document is not None and document.status == DocumentStatus.DELETING
    assert [(j.type, j.payload) for j in jobs] == [
        (JobType.DELETE_DOCUMENT, {"document_id": doc.id})
    ]
    shown = await faculty.client.get(response.headers["location"])
    assert "will be deleted shortly" in shown.text
    assert "notes.pdf" not in shown.text


async def test_reindex_form_queues_the_job(
    app: FastAPI, make_actor: ActorFactory, module: Module
) -> None:
    faculty = await make_actor(Role.FACULTY)
    doc = await insert_document(app, module_id=module.id, uploaded_by=faculty.user.id)
    response = await faculty.client.post(
        f"/modules/{module.id}/materials/{doc.id}/reindex",
        data={"csrf_token": faculty.csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("?notice=reindexing")
    async with app.state.database.read_session() as db:
        jobs = (await db.execute(select(Job))).scalars().all()
    assert [j.type for j in jobs] == [JobType.REINDEX_DOCUMENT]
    shown = await faculty.client.get(response.headers["location"])
    assert "queued for re-indexing" in shown.text
    # The document is pending now, so the page polls and offers no second re-index.
    assert (
        'hx-trigger="every 3s"' in shown.text
        and "/reindex" not in shown.text.split("<h2>Documents")[1]
    )


async def test_reindex_form_on_a_pending_document_shows_the_conflict(
    app: FastAPI, make_actor: ActorFactory, module: Module
) -> None:
    faculty = await make_actor(Role.FACULTY)
    doc = await insert_document(
        app, module_id=module.id, uploaded_by=faculty.user.id, status=DocumentStatus.PROCESSING
    )
    response = await faculty.client.post(
        f"/modules/{module.id}/materials/{doc.id}/reindex", data={"csrf_token": faculty.csrf}
    )
    assert response.status_code == 409
    assert "still being processed" in response.text
    assert 'class="alert alert-error"' in response.text


async def test_forms_reject_a_document_from_another_module(
    app: FastAPI, make_actor: ActorFactory, module: Module
) -> None:
    faculty = await make_actor(Role.FACULTY)
    async with app.state.database.read_session() as db:
        other = (await db.execute(select(Module).order_by(Module.number))).scalars().all()[1]
    doc = await insert_document(app, module_id=other.id, uploaded_by=faculty.user.id)
    for action in ("delete", "reindex"):
        response = await faculty.client.post(
            f"/modules/{module.id}/materials/{doc.id}/{action}",
            data={"csrf_token": faculty.csrf},
        )
        assert response.status_code == 404
    async with app.state.database.read_session() as db:
        assert (await db.get(Document, doc.id)).status == DocumentStatus.READY
        assert (await db.execute(select(Job))).scalars().all() == []


@pytest.mark.parametrize("action", ["delete", "reindex"])
async def test_form_roles(
    app: FastAPI, make_actor: ActorFactory, module: Module, action: str
) -> None:
    faculty = await make_actor(Role.FACULTY)
    doc = await insert_document(app, module_id=module.id, uploaded_by=faculty.user.id)
    url = f"/modules/{module.id}/materials/{doc.id}/{action}"
    for role in (Role.STUDENT, Role.ADMIN):
        actor = await make_actor(role)
        response = await actor.client.post(url, data={"csrf_token": actor.csrf})
        assert response.status_code == 403
    async with app.state.database.read_session() as db:
        assert (await db.execute(select(Job))).scalars().all() == []
        assert (await db.get(Document, doc.id)).status == DocumentStatus.READY


@pytest.mark.parametrize("action", ["delete", "reindex"])
async def test_forms_enforce_csrf_and_origin(
    app: FastAPI, make_actor: ActorFactory, module: Module, action: str
) -> None:
    faculty = await make_actor(Role.FACULTY)
    doc = await insert_document(app, module_id=module.id, uploaded_by=faculty.user.id)
    url = f"/modules/{module.id}/materials/{doc.id}/{action}"
    login_token = (await faculty.client.get("/api/v1/auth/csrf")).json()["csrf_token"]
    attempts: list[tuple[dict[str, str], dict[str, str]]] = [
        ({}, {}),
        ({"csrf_token": "wrong"}, {}),
        ({"csrf_token": login_token}, {}),
        ({"csrf_token": faculty.csrf}, {"Origin": "https://evil.example"}),
        ({"csrf_token": faculty.csrf}, {"Origin": "null"}),
        ({"csrf_token": faculty.csrf}, {"Origin": "", "Referer": "https://evil.example/p"}),
    ]
    for data, headers in attempts:
        response = await faculty.client.post(url, data=data, headers=headers)
        assert response.status_code == 403, (data, headers)
    async with app.state.database.read_session() as db:
        assert (await db.execute(select(Job))).scalars().all() == []


# --- inactive / must-change-password for every new page route --------------------------------


@pytest.mark.parametrize("state", ["inactive", "must_change_password"])
@pytest.mark.parametrize("kind", ["picker", "page", "fragment", "delete", "reindex"])
async def test_blocked_users_cannot_use_the_pages(
    app: FastAPI, make_actor: ActorFactory, module: Module, state: str, kind: str
) -> None:
    actor = await make_actor(Role.FACULTY)
    doc = await insert_document(app, module_id=module.id, uploaded_by=actor.user.id)
    async with app.state.database.session() as db:
        values: dict[str, Any] = (
            {"is_active": False} if state == "inactive" else {"must_change_password": True}
        )
        await db.execute(update(User).where(User.id == actor.user.id).values(**values))
        await db.commit()
    base = f"/modules/{module.id}/materials"
    requests = {
        "picker": ("GET", "/materials", None),
        "page": ("GET", base, None),
        "fragment": ("GET", f"{base}/documents", None),
        "delete": ("POST", f"{base}/{doc.id}/delete", {"csrf_token": actor.csrf}),
        "reindex": ("POST", f"{base}/{doc.id}/reindex", {"csrf_token": actor.csrf}),
    }
    method, path, data = requests[kind]
    response = await actor.client.request(method, path, data=data, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == ("/login" if state == "inactive" else "/change-password")
    async with app.state.database.read_session() as db:
        assert (await db.get(Document, doc.id)).status == DocumentStatus.READY
