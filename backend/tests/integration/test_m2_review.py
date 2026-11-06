"""Regression tests from the adversarial review of M2."""

import asyncio
import contextlib
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from app import worker
from app.core.config import Settings
from app.models import Document, Module, User
from app.models.enums import Role
from app.services import uploads
from tests.conftest import Actor, insert_document
from tests.files import make_docx, make_pdf, make_zip
from tests.integration.test_documents_api import (
    all_rows,
    stored_objects,
    upload,
    upload_url,
)

ActorFactory = Callable[..., Awaitable[Actor]]

HEAD = b'--x\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n\r\nsome data'
MULTIPART = {"Content-Type": "multipart/form-data; boundary=x"}


def temp_files(app: FastAPI) -> list[Path]:
    return list(Path(app.state.storage.root).rglob("*.part"))


@pytest.fixture
async def module_id(app: FastAPI, seeded: None) -> str:
    async with app.state.database.read_session() as db:
        module = (await db.execute(select(Module).order_by(Module.number))).scalars().first()
    assert isinstance(module, Module)
    return str(module.id)


async def test_object_is_deleted_when_the_post_store_zip_check_fails_unexpectedly(
    app: FastAPI,
    make_actor: ActorFactory,
    module_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An I/O error (disk full for the spool file...) after commit must not orphan the object."""

    async def boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(uploads, "check_stored_zip", boom)
    faculty = await make_actor(Role.FACULTY)
    response = await upload(faculty, module_id, ("a.docx", make_docx()))
    assert response.status_code == 500
    assert stored_objects(app) == []
    assert await all_rows(app, Document) == []


async def test_client_disconnect_mid_upload_leaves_nothing(
    app: FastAPI, make_actor: ActorFactory, module_id: str
) -> None:
    faculty = await make_actor(Role.FACULTY)

    async def dropped() -> AsyncIterator[bytes]:
        yield HEAD
        raise ConnectionError("client went away")

    with contextlib.suppress(Exception):  # the test transport may re-raise the client failure
        response = await faculty.client.post(
            upload_url(module_id),
            content=dropped(),
            headers={**faculty.headers, **MULTIPART},
        )
        assert response.status_code >= 400
    assert stored_objects(app) == []
    assert not temp_files(app)
    assert await all_rows(app, Document) == []


async def test_user_deactivated_mid_upload_cannot_register_the_file(
    app: FastAPI, make_actor: ActorFactory, module_id: str
) -> None:
    """The auth transaction is committed before streaming; the decision must be re-checked."""
    faculty = await make_actor(Role.FACULTY)

    async def body() -> AsyncIterator[bytes]:
        yield HEAD
        async with app.state.database.session() as db:
            await db.execute(update(User).where(User.id == faculty.user.id).values(is_active=False))
            await db.commit()
        yield b"\r\n--x--\r\n"

    response = await faculty.client.post(
        upload_url(module_id), content=body(), headers={**faculty.headers, **MULTIPART}
    )
    assert response.status_code in (401, 403)
    assert await all_rows(app, Document) == []
    assert stored_objects(app) == []


async def test_title_edit_strips_control_and_bidi_characters(
    app: FastAPI, make_actor: ActorFactory, module_id: str
) -> None:
    faculty = await make_actor(Role.FACULTY)
    doc = await insert_document(app, module_id=module_id, uploaded_by=faculty.user.id)
    response = await faculty.client.patch(
        f"/api/v1/documents/{doc.id}",
        json={"title": "gnp.\u202etxt\u0007 \u200bLecture"},
        headers=faculty.headers,
    )
    assert response.status_code == 200
    assert response.json()["title"] == "gnp.txt Lecture"


@pytest.mark.parametrize("name", ["C:/evil.xml", "c:\\evil.xml", "D:evil.xml"])
async def test_zip_member_names_with_drive_letters_are_rejected(
    make_actor: ActorFactory, module_id: str, name: str
) -> None:
    faculty = await make_actor(Role.FACULTY)
    data = make_zip({"[Content_Types].xml": b"<T/>", "word/document.xml": b"<d/>", name: b"x"})
    response = await upload(faculty, module_id, ("evil.docx", data))
    assert response.status_code == 415
    assert "unsafe" in response.json()["error"]["message"]


@pytest.mark.parametrize(
    "name",
    [
        "report.pdf:stream",
        "report.pdf::$DATA",
        "CON.pdf.exe",
        "evil\u202efdp.exe",
        "evil.pdf\u202e",
        "..",
        "...",
        " . ",
        "NUL",
    ],
)
async def test_windows_and_unicode_filename_tricks(
    app: FastAPI, make_actor: ActorFactory, module_id: str, name: str
) -> None:
    faculty = await make_actor(Role.FACULTY)
    response = await upload(faculty, module_id, (name, make_pdf()))
    if response.status_code == 202:  # only "evil.pdf" (after stripping the RLO) may pass
        assert response.json()["documents"][0]["original_filename"] == "evil.pdf"
    else:
        assert response.status_code == 415
        assert stored_objects(app) == []


async def test_inactive_user_cannot_download(
    make_actor: ActorFactory, module_id: str, app: FastAPI
) -> None:
    faculty = await make_actor(Role.FACULTY)
    doc = await insert_document(app, module_id=module_id, uploaded_by=faculty.user.id)
    student = await make_actor(Role.STUDENT)
    async with app.state.database.session() as db:
        await db.execute(update(User).where(User.id == student.user.id).values(is_active=False))
        await db.commit()
    assert (await student.client.get(f"/api/v1/documents/{doc.id}/download")).status_code == 401


async def test_worker_sweeps_stale_temp_files_at_startup(settings: Settings, app: FastAPI) -> None:
    tmp_dir = Path(app.state.storage.root) / ".tmp"
    tmp_dir.mkdir(parents=True)
    stale = tmp_dir / "old.part"
    fresh = tmp_dir / "new.part"
    stale.write_bytes(b"x")
    fresh.write_bytes(b"y")
    old = time.time() - 2 * 3600
    os.utime(stale, (old, old))
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run_worker(settings, stop, interval_s=0.05))
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert not stale.exists()
    assert fresh.exists()


async def test_htmx_poll_stops_when_nothing_is_pending(
    app: FastAPI, make_actor: ActorFactory, module_id: str
) -> None:
    faculty = await make_actor(Role.FACULTY)
    await insert_document(app, module_id=module_id, uploaded_by=faculty.user.id)
    html = (await faculty.client.get(f"/modules/{module_id}/materials/documents")).text
    assert "hx-trigger" not in html


async def test_chunked_body_without_content_length_hits_the_request_cap(
    app: FastAPI, settings: Settings, make_actor: ActorFactory, module_id: str
) -> None:
    settings.upload_max_mb = 1
    faculty = await make_actor(Role.FACULTY)

    async def huge_text_field() -> AsyncIterator[bytes]:
        yield b'--x\r\nContent-Disposition: form-data; name="note"\r\n\r\n'
        for _ in range(14):  # 14 MB of a non-file field: over the 11 MB request cap
            yield b"a" * (1024 * 1024)
        yield b"\r\n--x--\r\n"

    response = await faculty.client.post(
        upload_url(module_id), content=huge_text_field(), headers={**faculty.headers, **MULTIPART}
    )
    assert response.status_code == 413
    assert stored_objects(app) == []


async def test_many_tiny_parts_are_refused_but_earlier_files_are_kept(
    app: FastAPI, make_actor: ActorFactory, module_id: str
) -> None:
    faculty = await make_actor(Role.FACULTY)
    parts = [(f"note{i}", (None, "x")) for i in range(60)]
    files: list[Any] = [("file", ("ok.txt", b"fine", "text/plain")), *parts]
    response = await faculty.client.post(
        upload_url(module_id), files=files, headers=faculty.headers
    )
    assert response.status_code == 422
    assert len(stored_objects(app)) == 1  # the file before the flood was registered
