"""Upload, documents API, download and the upload route's body-limit exemption."""

import hashlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select, update

from app.core.config import Settings
from app.core.middleware import BODY_LIMIT_EXEMPT_PATTERNS
from app.models import AuditLog, Document, Job, Module, User
from app.models.enums import DocumentStatus, JobStatus, JobType, Role
from app.services import file_validation
from app.storage import LocalStorage
from tests.conftest import Actor, insert_document
from tests.files import SAMPLES, make_docx, make_pdf, make_pptx, make_zip

ActorFactory = Callable[..., Awaitable[Actor]]
Upload = tuple[str, bytes]


@pytest.fixture
async def module_id(app: FastAPI, seeded: None) -> str:
    async with app.state.database.read_session() as db:
        module = (await db.execute(select(Module).order_by(Module.number))).scalars().first()
    assert isinstance(module, Module)
    return str(module.id)


@pytest.fixture
async def other_module_id(app: FastAPI, seeded: None) -> str:
    async with app.state.database.read_session() as db:
        modules = (await db.execute(select(Module).order_by(Module.number))).scalars().all()
    return str(modules[1].id)


def upload_url(module_id: str) -> str:
    return f"/api/v1/modules/{module_id}/documents"


async def upload(
    actor: Actor,
    module_id: str,
    *files: Upload,
    headers: dict[str, str] | None = None,
) -> Any:
    parts = [("file", (name, data, "application/octet-stream")) for name, data in files]
    return await actor.client.post(
        upload_url(module_id),
        files=parts,
        headers=actor.headers if headers is None else headers,
    )


def stored_objects(app: FastAPI) -> list[Path]:
    storage = app.state.storage
    assert isinstance(storage, LocalStorage)
    if not storage.root.exists():
        return []
    return [p for p in storage.root.rglob("*") if p.is_file()]


async def all_rows(app: FastAPI, model: Any) -> list[Any]:
    async with app.state.database.read_session() as db:
        return list((await db.execute(select(model))).scalars().all())


# --- happy path ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("extension", "mime"),
    [
        ("pdf", "application/pdf"),
        ("pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("md", "text/markdown"),
        ("txt", "text/plain"),
        ("html", "text/html"),
    ],
)
async def test_each_allowed_type_is_accepted(
    app: FastAPI,
    make_actor: ActorFactory,
    module_id: str,
    extension: str,
    mime: str,
) -> None:
    faculty = await make_actor(Role.FACULTY)
    data = SAMPLES[extension]
    response = await upload(faculty, module_id, (f"Lecture 1.{extension}", data))
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["errors"] == []
    (doc,) = body["documents"]
    assert doc["title"] == "Lecture 1"
    assert doc["original_filename"] == f"Lecture 1.{extension}"
    assert doc["mime_type"] == mime  # detected server-side
    assert doc["size_bytes"] == len(data)
    assert doc["content_sha256"] == hashlib.sha256(data).hexdigest()
    assert doc["status"] == "uploaded"
    assert doc["module_id"] == module_id
    assert doc["uploaded_by"] == faculty.user.id
    # The stored object has the same bytes, under a random key (not the filename).
    (document,) = await all_rows(app, Document)
    assert len(document.storage_key) == 32 and "Lecture" not in document.storage_key
    stored = [p.read_bytes() for p in stored_objects(app)]
    assert stored == [data]


async def test_upload_enqueues_an_ids_only_job_and_audits(
    app: FastAPI, make_actor: ActorFactory, module_id: str
) -> None:
    faculty = await make_actor(Role.FACULTY)
    response = await upload(faculty, module_id, ("a.txt", b"hello"))
    doc_id = response.json()["documents"][0]["id"]
    (job,) = await all_rows(app, Job)
    assert job.type == JobType.INGEST_DOCUMENT
    assert job.payload == {"document_id": doc_id}
    assert job.status == JobStatus.QUEUED
    assert job.attempts == 0 and job.max_attempts >= 1
    audits = [a for a in await all_rows(app, AuditLog) if a.action == "document_uploaded"]
    assert len(audits) == 1
    assert audits[0].actor_id == faculty.user.id
    assert audits[0].target_type == "document" and audits[0].target_id == doc_id
    assert audits[0].details["filename"] == "a.txt"


async def test_upload_to_unknown_module_is_404_and_stores_nothing(
    app: FastAPI, make_actor: ActorFactory, seeded: None
) -> None:
    faculty = await make_actor(Role.FACULTY)
    response = await upload(faculty, "0" * 32, ("a.txt", b"hello"))
    assert response.status_code == 404
    assert stored_objects(app) == []


async def test_same_file_in_two_modules_is_allowed(
    app: FastAPI, make_actor: ActorFactory, module_id: str, other_module_id: str
) -> None:
    faculty = await make_actor(Role.FACULTY)
    assert (await upload(faculty, module_id, ("a.txt", b"same"))).status_code == 202
    assert (await upload(faculty, other_module_id, ("a.txt", b"same"))).status_code == 202
    assert len(await all_rows(app, Document)) == 2


# --- rejections ------------------------------------------------------------------------------


async def test_duplicate_in_same_module_is_409(
    app: FastAPI, make_actor: ActorFactory, module_id: str
) -> None:
    faculty = await make_actor(Role.FACULTY)
    first = await upload(faculty, module_id, ("a.txt", b"same content"))
    second = await upload(faculty, module_id, ("renamed.txt", b"same content"))
    assert second.status_code == 409
    error = second.json()["error"]
    assert error["code"] == "duplicate_document"
    assert error["details"]["existing_document_id"] == first.json()["documents"][0]["id"]
    assert len(await all_rows(app, Document)) == 1
    assert len(stored_objects(app)) == 1  # the duplicate's object was removed
    assert len(await all_rows(app, Job)) == 1


async def test_duplicate_of_a_document_being_deleted_still_conflicts(
    app: FastAPI, make_actor: ActorFactory, module_id: str
) -> None:
    faculty = await make_actor(Role.FACULTY)
    first = await upload(faculty, module_id, ("a.txt", b"same content"))
    doc_id = first.json()["documents"][0]["id"]
    assert (
        await faculty.client.delete(f"/api/v1/documents/{doc_id}", headers=faculty.headers)
    ).status_code == 202
    again = await upload(faculty, module_id, ("a.txt", b"same content"))
    assert again.status_code == 409
    assert "being deleted" in again.json()["error"]["message"]


@pytest.mark.parametrize(
    "name", ["virus.exe", "script.js", "archive.zip", "noextension", "x.PDF.exe"]
)
async def test_disallowed_extension_is_415(
    app: FastAPI, make_actor: ActorFactory, module_id: str, name: str
) -> None:
    faculty = await make_actor(Role.FACULTY)
    response = await upload(faculty, module_id, (name, make_pdf()))
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_file_type"
    assert stored_objects(app) == []
    assert await all_rows(app, Document) == []


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("fake.pdf", make_zip({"a": b"b"})),  # .pdf that is a zip
        ("fake.pdf", b"just text"),
        ("fake.pdf", b"   %PDF-1.4"),
        ("fake.pptx", make_pdf()),  # .pptx that is a PDF
        ("fake.docx", make_zip({"[Content_Types].xml": b"x"})),  # zip without word/document.xml
        ("fake.docx", make_pptx()),  # a pptx renamed to .docx
        ("fake.pptx", make_docx()),
        ("fake.docx", b"PK\x03\x04 truncated garbage"),
        ("nul.txt", b"abc\x00def"),
        ("nul.md", b"# title\x00"),
        ("bad.html", b"<p>\xff\xfe</p>"),
        ("bad.txt", "caf\u00e9".encode("latin-1")),
    ],
)
async def test_extension_content_mismatch_is_415(
    app: FastAPI, make_actor: ActorFactory, module_id: str, name: str, data: bytes
) -> None:
    faculty = await make_actor(Role.FACULTY)
    response = await upload(faculty, module_id, (name, data))
    assert response.status_code == 415, response.text
    assert response.json()["error"]["code"] == "unsupported_file_type"
    assert stored_objects(app) == []
    assert await all_rows(app, Document) == []
    assert await all_rows(app, Job) == []


async def test_empty_file_is_rejected(
    app: FastAPI, make_actor: ActorFactory, module_id: str
) -> None:
    faculty = await make_actor(Role.FACULTY)
    response = await upload(faculty, module_id, ("empty.txt", b""))
    assert response.status_code == 422
    assert stored_objects(app) == []


async def test_zip_bomb_is_rejected_and_cleaned_up(
    app: FastAPI,
    make_actor: ActorFactory,
    module_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(file_validation, "MAX_UNCOMPRESSED_BYTES", 1024 * 1024)
    bomb = make_zip(
        {
            "[Content_Types].xml": b"<Types/>",
            "word/document.xml": b"<d/>",
            "word/payload.bin": b"\x00" * (4 * 1024 * 1024),
        }
    )
    assert len(bomb) < 100_000
    faculty = await make_actor(Role.FACULTY)
    response = await upload(faculty, module_id, ("bomb.docx", bomb))
    assert response.status_code == 415
    assert "zip bomb" in response.json()["error"]["message"]
    assert stored_objects(app) == []  # the committed object was deleted again
    assert await all_rows(app, Document) == []


async def test_oversized_upload_is_aborted_while_streaming(
    app: FastAPI, settings: Settings, make_actor: ActorFactory, module_id: str
) -> None:
    settings.upload_max_mb = 1
    faculty = await make_actor(Role.FACULTY)
    big = make_pdf() + b"\x00" * (2 * 1024 * 1024)
    response = await upload(faculty, module_id, ("big.pdf", big))
    assert response.status_code == 413
    error = response.json()["error"]
    assert error["code"] == "payload_too_large"
    assert error["details"]["max_mb"] == 1
    assert stored_objects(app) == []  # no object, no temp file
    assert await all_rows(app, Document) == []
    assert await all_rows(app, Job) == []


async def test_file_exactly_at_the_limit_is_accepted(
    app: FastAPI, settings: Settings, make_actor: ActorFactory, module_id: str
) -> None:
    settings.upload_max_mb = 1
    faculty = await make_actor(Role.FACULTY)
    data = make_pdf()
    data += b"\x00" * (1024 * 1024 - len(data))
    assert len(data) == 1024 * 1024
    assert (await upload(faculty, module_id, ("limit.pdf", data))).status_code == 202


async def test_declared_content_length_over_the_request_cap_is_413(
    app: FastAPI, settings: Settings, make_actor: ActorFactory, module_id: str
) -> None:
    faculty = await make_actor(Role.FACULTY)
    headers = {
        **faculty.headers,
        "Content-Type": "multipart/form-data; boundary=x",
        "Content-Length": str(10**12),
    }
    response = await faculty.client.post(upload_url(module_id), content=b"", headers=headers)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


@pytest.mark.parametrize(
    "headers",
    [{"Content-Type": "application/json"}, {"Content-Type": "multipart/form-data"}, {}],
)
async def test_non_multipart_body_is_422(
    make_actor: ActorFactory, module_id: str, headers: dict[str, str]
) -> None:
    faculty = await make_actor(Role.FACULTY)
    response = await faculty.client.post(
        upload_url(module_id), content=b"{}", headers={**faculty.headers, **headers}
    )
    assert response.status_code == 422


async def test_multipart_without_files_is_422(make_actor: ActorFactory, module_id: str) -> None:
    faculty = await make_actor(Role.FACULTY)
    response = await faculty.client.post(
        upload_url(module_id), files={"note": (None, "hello")}, headers=faculty.headers
    )
    assert response.status_code == 422
    assert "No file" in response.json()["error"]["message"]


async def test_malformed_multipart_is_400(make_actor: ActorFactory, module_id: str) -> None:
    faculty = await make_actor(Role.FACULTY)
    response = await faculty.client.post(
        upload_url(module_id),
        content=b"--x\r\nthis is not a header\r\n\r\ndata--x--",
        headers={**faculty.headers, "Content-Type": "multipart/form-data; boundary=x"},
    )
    assert response.status_code in (400, 422)


async def test_truncated_multipart_stores_nothing(
    app: FastAPI, make_actor: ActorFactory, module_id: str
) -> None:
    faculty = await make_actor(Role.FACULTY)
    body = (
        b'--x\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n\r\n'
        b"partial content that never gets a closing boundary"
    )
    response = await faculty.client.post(
        upload_url(module_id),
        content=body,
        headers={**faculty.headers, "Content-Type": "multipart/form-data; boundary=x"},
    )
    assert response.status_code in (400, 422)
    assert stored_objects(app) == []
    assert await all_rows(app, Document) == []


# --- multiple files --------------------------------------------------------------------------


async def test_multi_file_upload_reports_each_file(
    app: FastAPI, make_actor: ActorFactory, module_id: str
) -> None:
    faculty = await make_actor(Role.FACULTY)
    first = await upload(faculty, module_id, ("seen.txt", b"seen"))
    assert first.status_code == 202
    response = await upload(
        faculty,
        module_id,
        ("good.pdf", make_pdf("1")),
        ("bad.exe", b"MZ"),
        ("fake.docx", b"not a zip"),
        ("seen-again.txt", b"seen"),
        ("good2.md", b"# ok"),
    )
    assert response.status_code == 202
    body = response.json()
    assert [d["original_filename"] for d in body["documents"]] == ["good.pdf", "good2.md"]
    errors = {e["filename"]: e for e in body["errors"]}
    assert (
        errors["bad.exe"]["status"] == 415 and errors["bad.exe"]["code"] == "unsupported_file_type"
    )
    assert errors["fake.docx"]["status"] == 415
    assert errors["seen-again.txt"]["status"] == 409
    assert errors["seen-again.txt"]["code"] == "duplicate_document"
    assert len(await all_rows(app, Document)) == 3  # seen.txt + the two good ones
    assert len(stored_objects(app)) == 3
    assert len(await all_rows(app, Job)) == 3


async def test_multi_file_all_bad_uses_the_first_status_and_lists_all_errors(
    app: FastAPI, make_actor: ActorFactory, module_id: str
) -> None:
    faculty = await make_actor(Role.FACULTY)
    response = await upload(faculty, module_id, ("a.exe", b"x"), ("b.txt", b"x\x00"))
    assert response.status_code == 415
    errors = response.json()["error"]["details"]["errors"]
    assert [e["filename"] for e in errors] == ["a.exe", "b.txt"]
    assert stored_objects(app) == []


async def test_oversize_file_ends_the_request_but_keeps_earlier_files(
    app: FastAPI, settings: Settings, make_actor: ActorFactory, module_id: str
) -> None:
    settings.upload_max_mb = 1
    faculty = await make_actor(Role.FACULTY)
    response = await upload(
        faculty,
        module_id,
        ("ok.txt", b"fine"),
        ("big.pdf", make_pdf() + b"\x00" * (2 * 1024 * 1024)),
        ("after.txt", b"never read"),
    )
    assert response.status_code == 202
    body = response.json()
    assert [d["original_filename"] for d in body["documents"]] == ["ok.txt"]
    assert [(e["filename"], e["status"]) for e in body["errors"]] == [("big.pdf", 413)]
    assert len(stored_objects(app)) == 1


async def test_too_many_files_in_one_request(
    app: FastAPI, make_actor: ActorFactory, module_id: str
) -> None:
    faculty = await make_actor(Role.FACULTY)
    files = [(f"f{i}.txt", f"content {i}".encode()) for i in range(12)]
    response = await upload(faculty, module_id, *files)
    assert response.status_code == 202
    body = response.json()
    assert len(body["documents"]) == 10
    assert {e["code"] for e in body["errors"]} == {"too_many_files"}
    assert len(body["errors"]) == 2


# --- filenames -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sent", "stored"),
    [
        ("../../etc/passwd.txt", "passwd.txt"),
        ("..\\..\\boot\\evil.txt", "evil.txt"),
        ("C:\\fakepath\\notes.md", "notes.md"),
        ("sub/dir/slides.html", "slides.html"),
        ("r\u00e9sum\u00e9 \u8bb2\u4e49.txt", "r\u00e9sum\u00e9 \u8bb2\u4e49.txt"),
        ("<img src=x onerror=alert(1)>.txt", "<img src=x onerror=alert(1)>.txt"),
    ],
)
async def test_filenames_are_sanitised(
    app: FastAPI, make_actor: ActorFactory, module_id: str, sent: str, stored: str
) -> None:
    faculty = await make_actor(Role.FACULTY)
    response = await upload(faculty, module_id, (sent, b"content"))
    assert response.status_code == 202, response.text
    (doc,) = response.json()["documents"]
    assert doc["original_filename"] == stored
    # Display data only: the storage key is random hex regardless of the name.
    (document,) = await all_rows(app, Document)
    assert len(document.storage_key) == 32
    assert all(p.name == document.storage_key for p in stored_objects(app))


async def test_very_long_filename_is_truncated_keeping_the_extension(
    make_actor: ActorFactory, module_id: str
) -> None:
    faculty = await make_actor(Role.FACULTY)
    response = await upload(faculty, module_id, ("n" * 600 + ".txt", b"content"))
    assert response.status_code == 202
    (doc,) = response.json()["documents"]
    assert len(doc["original_filename"]) <= 200 and doc["original_filename"].endswith(".txt")
    assert len(doc["title"]) <= 300


# --- failures after the object was stored ----------------------------------------------------


async def test_object_is_deleted_when_the_db_insert_fails(
    app: FastAPI,
    make_actor: ActorFactory,
    module_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import uploads

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("database exploded")

    monkeypatch.setattr(uploads, "enqueue", boom)
    faculty = await make_actor(Role.FACULTY)
    response = await upload(faculty, module_id, ("a.txt", b"hello"))
    assert response.status_code == 500
    assert stored_objects(app) == []
    assert await all_rows(app, Document) == []
    assert await all_rows(app, Job) == []


async def test_unique_violation_race_deletes_the_object(
    app: FastAPI,
    make_actor: ActorFactory,
    module_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two identical uploads that both pass the duplicate pre-check: the DB constraint decides."""
    from app.services import uploads

    faculty = await make_actor(Role.FACULTY)
    await insert_document(
        app,
        module_id=module_id,
        uploaded_by=faculty.user.id,
        content=b"racing",
        filename="first.txt",
        mime_type="text/plain",
    )
    real_register = uploads._UploadReceiver._register

    class _NoRow:
        def first(self) -> None:
            return None

    async def register_without_precheck(self: Any, part: Any) -> None:
        real_execute = self.db.execute
        calls = 0

        async def execute(*args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 2:  # (1 is the still-authorized check) the pre-check "sees" nothing
                return _NoRow()
            return await real_execute(*args, **kwargs)

        self.db.execute = execute
        await real_register(self, part)

    monkeypatch.setattr(uploads._UploadReceiver, "_register", register_without_precheck)
    response = await upload(faculty, module_id, ("second.txt", b"racing"))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "duplicate_document"
    assert len(stored_objects(app)) == 1  # only the pre-existing object remains


# --- authorization / CSRF --------------------------------------------------------------------


async def test_upload_roles(app: FastAPI, make_actor: ActorFactory, module_id: str) -> None:
    student = await make_actor(Role.STUDENT)
    assert (await upload(student, module_id, ("a.txt", b"x"))).status_code == 403
    admin = await make_actor(Role.ADMIN)
    assert (await upload(admin, module_id, ("a.txt", b"x"))).status_code == 403
    assert stored_objects(app) == []
    faculty = await make_actor(Role.FACULTY)
    assert (await upload(faculty, module_id, ("a.txt", b"x"))).status_code == 202
    assert len(stored_objects(app)) == 1  # only the faculty upload was stored


async def test_upload_anonymous_is_401(client: AsyncClient, module_id: str) -> None:
    response = await client.post(
        upload_url(module_id), files=[("file", ("a.txt", b"x", "text/plain"))]
    )
    assert response.status_code == 401


async def test_upload_requires_csrf_and_same_origin(
    app: FastAPI, make_actor: ActorFactory, module_id: str
) -> None:
    faculty = await make_actor(Role.FACULTY)
    for headers in (
        {},
        {"X-CSRF-Token": "wrong"},
        {"X-CSRF-Token": (await faculty.client.get("/api/v1/auth/csrf")).json()["csrf_token"]},
        {**faculty.headers, "Origin": "https://evil.example"},
        {**faculty.headers, "Origin": "null"},
        {**faculty.headers, "Origin": "", "Referer": "https://evil.example/x"},
    ):
        response = await upload(faculty, module_id, ("a.txt", b"x"), headers=headers)
        assert response.status_code == 403, headers
    assert stored_objects(app) == []
    assert await all_rows(app, Document) == []


async def test_csrf_form_field_does_not_authorize_a_multipart_upload(
    app: FastAPI, make_actor: ActorFactory, module_id: str
) -> None:
    """The multipart body is never parsed for a token: only the header counts."""
    faculty = await make_actor(Role.FACULTY)
    response = await faculty.client.post(
        upload_url(module_id),
        data={"csrf_token": faculty.csrf},
        files=[("file", ("a.txt", b"x", "text/plain"))],
    )
    assert response.status_code == 403
    assert stored_objects(app) == []


# --- every new API route: role, inactive and must-change-password matrix ---------------------


@pytest.fixture
async def doc_id(app: FastAPI, module_id: str, make_actor: ActorFactory) -> str:
    uploader = await make_actor(Role.FACULTY, email="owner@example.edu")
    document = await insert_document(app, module_id=module_id, uploaded_by=uploader.user.id)
    return document.id


def api_calls(module_id: str, doc_id: str) -> list[tuple[str, str]]:
    api = "/api/v1"
    return [
        ("GET", f"{api}/modules/{module_id}/documents"),
        ("GET", f"{api}/documents/{doc_id}"),
        ("GET", f"{api}/documents/{doc_id}/download"),
        ("PATCH", f"{api}/documents/{doc_id}"),
        ("POST", f"{api}/documents/{doc_id}/reindex"),
        ("DELETE", f"{api}/documents/{doc_id}"),
    ]


@pytest.mark.parametrize("index", range(6))
@pytest.mark.parametrize("state", ["inactive", "must_change_password"])
async def test_inactive_and_pending_password_users_are_blocked(
    app: FastAPI,
    make_actor: ActorFactory,
    module_id: str,
    doc_id: str,
    index: int,
    state: str,
) -> None:
    actor = await make_actor(Role.FACULTY)
    async with app.state.database.session() as db:
        values = {"is_active": False} if state == "inactive" else {"must_change_password": True}
        await db.execute(update(User).where(User.id == actor.user.id).values(**values))
        await db.commit()
    method, path = api_calls(module_id, doc_id)[index]
    kwargs: dict[str, Any] = {"headers": actor.headers}
    if method == "PATCH":
        kwargs["json"] = {"title": "x"}
    response = await actor.client.request(method, path, **kwargs)
    assert response.status_code == (401 if state == "inactive" else 403), (method, path)
    if state == "must_change_password":
        assert response.json()["error"]["code"] == "password_change_required"
    # And the upload itself.
    uploaded = await upload(actor, module_id, ("a.txt", b"x"))
    assert uploaded.status_code == (401 if state == "inactive" else 403)
    assert len(stored_objects(app)) == 1  # only the fixture document's object
    assert len(await all_rows(app, Document)) == 1


@pytest.mark.parametrize(
    ("method", "suffix", "payload"),
    [
        ("PATCH", "", {"title": "Changed"}),
        ("POST", "/reindex", None),
        ("DELETE", "", None),
    ],
)
async def test_faculty_document_routes_enforce_csrf_and_origin(
    make_actor: ActorFactory,
    doc_id: str,
    method: str,
    suffix: str,
    payload: dict[str, Any] | None,
) -> None:
    faculty = await make_actor(Role.FACULTY)
    path = f"/api/v1/documents/{doc_id}{suffix}"

    async def call(headers: dict[str, str]) -> int:
        kwargs: dict[str, Any] = {"headers": headers}
        if payload is not None:
            kwargs["json"] = payload
        return (await faculty.client.request(method, path, **kwargs)).status_code

    assert await call({}) == 403
    assert await call({"X-CSRF-Token": "bad"}) == 403
    assert await call({**faculty.headers, "Origin": "https://evil.example"}) == 403
    assert await call({**faculty.headers, "Origin": "null"}) == 403
    assert await call(faculty.headers) in (200, 202)  # and it works with both


# --- list / get / patch / delete / reindex ---------------------------------------------------


async def test_list_excludes_deleting_and_orders_newest_first(
    app: FastAPI, make_actor: ActorFactory, module_id: str, other_module_id: str
) -> None:
    faculty = await make_actor(Role.FACULTY)
    student = await make_actor(Role.STUDENT)
    a = (await upload(faculty, module_id, ("a.txt", b"a"))).json()["documents"][0]
    b = (await upload(faculty, module_id, ("b.txt", b"b"))).json()["documents"][0]
    await upload(faculty, other_module_id, ("c.txt", b"c"))
    listed = await student.client.get(upload_url(module_id))
    assert listed.status_code == 200
    assert [d["id"] for d in listed.json()] == [b["id"], a["id"]]
    await faculty.client.delete(f"/api/v1/documents/{a['id']}", headers=faculty.headers)
    listed = await student.client.get(upload_url(module_id))
    assert [d["id"] for d in listed.json()] == [b["id"]]
    assert (await student.client.get(f"/api/v1/modules/{'0' * 32}/documents")).status_code == 404


async def test_get_document_and_404s(make_actor: ActorFactory, doc_id: str) -> None:
    student = await make_actor(Role.STUDENT)
    response = await student.client.get(f"/api/v1/documents/{doc_id}")
    assert response.status_code == 200 and response.json()["id"] == doc_id
    assert (await student.client.get(f"/api/v1/documents/{'0' * 32}")).status_code == 404


async def test_patch_updates_title_and_week_with_validation(
    app: FastAPI, make_actor: ActorFactory, doc_id: str
) -> None:
    faculty = await make_actor(Role.FACULTY)
    url = f"/api/v1/documents/{doc_id}"
    ok = await faculty.client.patch(
        url, json={"title": "  Lecture   3  ", "week": " Week 3 "}, headers=faculty.headers
    )
    assert ok.status_code == 200
    assert ok.json()["title"] == "Lecture 3" and ok.json()["week"] == "Week 3"
    only_title = await faculty.client.patch(url, json={"title": "T"}, headers=faculty.headers)
    assert only_title.json()["week"] == "Week 3"  # untouched
    cleared = await faculty.client.patch(url, json={"week": ""}, headers=faculty.headers)
    assert cleared.json()["week"] is None
    for bad in ({"title": ""}, {"title": "   "}, {"title": "x" * 301}, {"week": "w" * 61}):
        response = await faculty.client.patch(url, json=bad, headers=faculty.headers)
        assert response.status_code == 422, bad
    assert (
        await faculty.client.patch(url, json={"title": "x" * 501}, headers=faculty.headers)
    ).status_code == 422
    audits = [a for a in await all_rows(app, AuditLog) if a.action == "document_updated"]
    assert len(audits) == 3  # the invalid ones changed nothing
    assert (await faculty.client.get(url)).json()["title"] == "T"


async def test_patch_unknown_fields_are_ignored_and_status_is_not_editable(
    make_actor: ActorFactory, doc_id: str
) -> None:
    faculty = await make_actor(Role.FACULTY)
    url = f"/api/v1/documents/{doc_id}"
    response = await faculty.client.patch(
        url,
        json={"status": "failed", "storage_key": "x", "module_id": "y"},
        headers=faculty.headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready" and body["module_id"] != "y"


async def test_delete_hides_the_document_and_enqueues_a_job(
    app: FastAPI, make_actor: ActorFactory, module_id: str, doc_id: str
) -> None:
    faculty = await make_actor(Role.FACULTY)
    response = await faculty.client.delete(f"/api/v1/documents/{doc_id}", headers=faculty.headers)
    assert response.status_code == 202
    assert response.json()["status"] == "deleting"
    (job,) = await all_rows(app, Job)
    assert job.type == JobType.DELETE_DOCUMENT and job.payload == {"document_id": doc_id}
    assert job.status == JobStatus.QUEUED
    (document,) = await all_rows(app, Document)
    assert document.status == DocumentStatus.DELETING  # the row stays until the worker runs
    assert len(stored_objects(app)) == 1  # so does the file
    audits = [a for a in await all_rows(app, AuditLog) if a.action == "document_delete_requested"]
    assert len(audits) == 1 and audits[0].target_id == doc_id
    # Hidden everywhere, and not deletable twice.
    assert (await faculty.client.get(f"/api/v1/documents/{doc_id}")).status_code == 404
    assert (await faculty.client.get(upload_url(module_id))).json() == []
    assert (await faculty.client.get(f"/api/v1/documents/{doc_id}/download")).status_code == 404
    assert (
        await faculty.client.delete(f"/api/v1/documents/{doc_id}", headers=faculty.headers)
    ).status_code == 404
    assert (
        await faculty.client.post(f"/api/v1/documents/{doc_id}/reindex", headers=faculty.headers)
    ).status_code == 404
    assert len(await all_rows(app, Job)) == 1


async def test_reindex_enqueues_a_job_and_resets_status(
    app: FastAPI, make_actor: ActorFactory, doc_id: str
) -> None:
    faculty = await make_actor(Role.FACULTY)
    response = await faculty.client.post(
        f"/api/v1/documents/{doc_id}/reindex", headers=faculty.headers
    )
    assert response.status_code == 202
    assert response.json()["status"] == "uploaded"
    (job,) = await all_rows(app, Job)
    assert job.type == JobType.REINDEX_DOCUMENT and job.payload == {"document_id": doc_id}
    audits = [a for a in await all_rows(app, AuditLog) if a.action == "document_reindex_requested"]
    assert len(audits) == 1
    # Already queued: a second request is a conflict and does not add another job.
    again = await faculty.client.post(
        f"/api/v1/documents/{doc_id}/reindex", headers=faculty.headers
    )
    assert again.status_code == 409
    assert len(await all_rows(app, Job)) == 1


@pytest.mark.parametrize("status", [DocumentStatus.UPLOADED, DocumentStatus.PROCESSING])
async def test_reindex_of_pending_documents_conflicts(
    app: FastAPI, make_actor: ActorFactory, module_id: str, status: DocumentStatus
) -> None:
    faculty = await make_actor(Role.FACULTY)
    document = await insert_document(
        app, module_id=module_id, uploaded_by=faculty.user.id, status=status
    )
    response = await faculty.client.post(
        f"/api/v1/documents/{document.id}/reindex", headers=faculty.headers
    )
    assert response.status_code == 409
    assert await all_rows(app, Job) == []


async def test_reindex_of_a_failed_document_clears_the_error(
    app: FastAPI, make_actor: ActorFactory, module_id: str
) -> None:
    faculty = await make_actor(Role.FACULTY)
    document = await insert_document(
        app, module_id=module_id, uploaded_by=faculty.user.id, status=DocumentStatus.FAILED
    )
    async with app.state.database.session() as db:
        await db.execute(update(Document).where(Document.id == document.id).values(error="boom"))
        await db.commit()
    response = await faculty.client.post(
        f"/api/v1/documents/{document.id}/reindex", headers=faculty.headers
    )
    assert response.status_code == 202
    assert response.json()["error"] is None


# --- download --------------------------------------------------------------------------------


async def test_pdf_download_is_inline_with_safe_headers(
    make_actor: ActorFactory, app: FastAPI, module_id: str
) -> None:
    faculty = await make_actor(Role.FACULTY)
    student = await make_actor(Role.STUDENT)
    data = make_pdf("body")
    doc = (await upload(faculty, module_id, ("Lecture 1.pdf", data))).json()["documents"][0]
    response = await student.client.get(f"/api/v1/documents/{doc['id']}/download")
    assert response.status_code == 200
    assert response.content == data
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-length"] == str(len(data))
    assert response.headers["content-disposition"].startswith("inline;")
    assert 'filename="Lecture 1.pdf"' in response.headers["content-disposition"]
    assert "filename*=UTF-8''Lecture%201.pdf" in response.headers["content-disposition"]
    assert response.headers["x-content-type-options"] == "nosniff"
    # Chromium's PDF viewer does not render in a sandboxed document: inline PDFs get a CSP that
    # forbids everything instead of `sandbox`.
    csp = response.headers["content-security-policy"]
    assert "sandbox" not in csp
    assert csp.startswith("default-src 'none'; object-src 'none'; script-src 'none'")
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("name", "mime"),
    [
        (
            "slides.pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ),
        ("notes.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("readme.md", "text/markdown"),
        ("plain.txt", "text/plain"),
        ("page.html", "text/html"),
    ],
)
async def test_non_pdf_downloads_are_attachments(
    make_actor: ActorFactory, module_id: str, name: str, mime: str
) -> None:
    faculty = await make_actor(Role.FACULTY)
    data = SAMPLES[name.rsplit(".", 1)[1]]
    doc = (await upload(faculty, module_id, (name, data))).json()["documents"][0]
    response = await faculty.client.get(f"/api/v1/documents/{doc['id']}/download")
    assert response.status_code == 200 and response.content == data
    assert response.headers["content-type"].split(";")[0] == mime
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "sandbox"


async def test_download_encodes_unusual_filenames(make_actor: ActorFactory, module_id: str) -> None:
    faculty = await make_actor(Role.FACULTY)
    name = "r\u00e9sum\u00e9 (q) \u8bb2\u4e49;x.txt"
    doc = (await upload(faculty, module_id, (name, b"hello"))).json()["documents"][0]
    response = await faculty.client.get(f"/api/v1/documents/{doc['id']}/download")
    header = response.headers["content-disposition"]
    assert header.startswith("attachment; filename=")
    assert header.isascii()  # no raw non-ASCII in a header
    fallback = header.split('filename="', 1)[1].split('"', 1)[0]
    assert "\u00e9" not in fallback and "\u8bb2" not in fallback
    assert "filename*=UTF-8''r%C3%A9sum%C3%A9%20%28q%29%20%E8%AE%B2%E4%B9%89%3Bx.txt" in header


async def test_download_of_html_is_never_inline(make_actor: ActorFactory, module_id: str) -> None:
    faculty = await make_actor(Role.FACULTY)
    doc = (await upload(faculty, module_id, ("x.html", b"<script>alert(1)</script>"))).json()[
        "documents"
    ][0]
    response = await faculty.client.get(f"/api/v1/documents/{doc['id']}/download")
    assert response.headers["content-disposition"].startswith("attachment")
    assert "sandbox" in response.headers["content-security-policy"]


async def test_download_unknown_document_and_missing_object_are_404(
    app: FastAPI, make_actor: ActorFactory, module_id: str
) -> None:
    faculty = await make_actor(Role.FACULTY)
    assert (await faculty.client.get(f"/api/v1/documents/{'0' * 32}/download")).status_code == 404
    ghost = await insert_document(
        app, module_id=module_id, uploaded_by=faculty.user.id, write_object=False
    )
    assert (await faculty.client.get(f"/api/v1/documents/{ghost.id}/download")).status_code == 404


async def test_other_responses_keep_the_standard_csp(make_actor: ActorFactory, doc_id: str) -> None:
    student = await make_actor(Role.STUDENT)
    response = await student.client.get(f"/api/v1/documents/{doc_id}")
    assert "script-src 'self'" in response.headers["content-security-policy"]


# --- body-limit exemption --------------------------------------------------------------------


def test_exemption_patterns_match_only_the_upload_route() -> None:
    def exempt(path: str) -> bool:
        return any(p.match(path) for p in BODY_LIMIT_EXEMPT_PATTERNS)

    mid = "a" * 32
    assert exempt(f"/api/v1/modules/{mid}/documents")
    for path in (
        f"/api/v1/modules/{mid}/documents/",
        f"/api/v1/modules/{mid}/documents/extra",
        f"/api/v1/modules/{mid}",
        f"/api/v1/modules/{mid}/documents/../x",
        f"/api/v1/modules/{mid}/other/documents",
        f"/api/v1/modules/{mid}/documents%2f",
        f"/x/api/v1/modules/{mid}/documents",
        f"/api/v1/documents/{mid}",
        f"/api/v1/documents/{mid}/reindex",
        "/api/v1/modules/../documents",
        "/api/v1/modules//documents",
        "/api/v1/auth/login",
        "/",
    ):
        assert not exempt(path), path


async def test_global_body_limit_still_applies_elsewhere(
    make_actor: ActorFactory, module_id: str, doc_id: str
) -> None:
    faculty = await make_actor(Role.FACULTY)
    big = "x" * (3 * 1024 * 1024)  # over the 2 MB default
    patch = await faculty.client.patch(
        f"/api/v1/documents/{doc_id}", json={"title": big}, headers=faculty.headers
    )
    assert patch.status_code == 413
    other = await faculty.client.patch(
        f"/api/v1/modules/{module_id}", json={"description": big}, headers=faculty.headers
    )
    assert other.status_code == 413


async def test_upload_route_accepts_bodies_over_the_global_limit(
    app: FastAPI, make_actor: ActorFactory, module_id: str
) -> None:
    faculty = await make_actor(Role.FACULTY)
    data = make_pdf() + b"\x01" * (3 * 1024 * 1024)  # 3 MB: over MAX_REQUEST_BODY_MB, under 50
    response = await upload(faculty, module_id, ("three.pdf", data))
    assert response.status_code == 202
    assert response.json()["documents"][0]["size_bytes"] == len(data)
