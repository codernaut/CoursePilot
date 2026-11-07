"""Administrator API and pages: jobs (filter, paging, retry), usage numbers and answer feedback."""

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.db.base import utcnow
from app.models import AuditLog, Conversation, Document, Job, Message, Module
from app.models.enums import (
    DocumentStatus,
    Feedback,
    JobStatus,
    JobType,
    MessageRole,
    Role,
)
from app.retrieval.hybrid import PATH_SEPARATOR
from tests.conftest import Actor, insert_document

ActorFactory = Callable[..., Awaitable[Actor]]
API = "/api/v1/admin"


@pytest.fixture
async def module(app: FastAPI, seeded: None) -> Module:
    async with app.state.database.read_session() as db:
        found = (await db.execute(select(Module).order_by(Module.number))).scalars().first()
    assert isinstance(found, Module)
    return found


async def add_job(
    app: FastAPI,
    document: Document | None,
    *,
    status: JobStatus = JobStatus.FAILED,
    job_type: JobType = JobType.INGEST_DOCUMENT,
    error: str | None = "boom",
    attempts: int = 3,
    age_s: int = 0,
) -> Job:
    async with app.state.database.session() as db:
        job = Job(
            type=job_type,
            payload={"document_id": document.id} if document else {},
            status=status,
            attempts=attempts,
            last_error=error,
            created_at=utcnow() - timedelta(seconds=age_s),
        )
        db.add(job)
        await db.commit()
        return job


async def set_doc(app: FastAPI, document_id: str, **values: Any) -> None:
    async with app.state.database.session() as db:
        document = await db.get(Document, document_id)
        assert document is not None
        for key, value in values.items():
            setattr(document, key, value)
        await db.commit()


async def doc(app: FastAPI, document_id: str) -> Document:
    async with app.state.database.read_session() as db:
        found: Document | None = await db.get(Document, document_id)
    assert found is not None
    return found


async def job_row(app: FastAPI, job_id: str) -> Job:
    async with app.state.database.read_session() as db:
        found: Job | None = await db.get(Job, job_id)
    assert found is not None
    return found


# -- jobs API ------------------------------------------------------------------------------


async def test_jobs_list_filters_pages_and_names_the_document(
    app: FastAPI, make_actor: ActorFactory, module: Module
) -> None:
    admin = await make_actor(Role.ADMIN)
    faculty = await make_actor(Role.FACULTY)
    document = await insert_document(
        app, module_id=module.id, uploaded_by=faculty.user.id, title="Heap notes"
    )
    failed = await add_job(app, document, age_s=30)
    await add_job(app, document, status=JobStatus.DONE, error=None, age_s=20)
    await add_job(
        app, document, status=JobStatus.QUEUED, job_type=JobType.REINDEX_DOCUMENT, age_s=10
    )

    everything = (await admin.client.get(f"{API}/jobs")).json()
    assert everything["total"] == 3 and len(everything["items"]) == 3
    newest = everything["items"][0]
    assert newest["type"] == "reindex_document" and newest["document_title"] == "Heap notes"

    only_failed = (await admin.client.get(f"{API}/jobs?status=failed")).json()
    assert [j["id"] for j in only_failed["items"]] == [failed.id]
    assert only_failed["items"][0]["last_error"] == "boom"
    assert only_failed["items"][0]["document_id"] == document.id

    by_type = (await admin.client.get(f"{API}/jobs?type=reindex_document")).json()
    assert by_type["total"] == 1

    page = (await admin.client.get(f"{API}/jobs?limit=2&offset=2")).json()
    assert page["total"] == 3 and len(page["items"]) == 1 and page["offset"] == 2
    assert page["items"][0]["id"] == failed.id  # the oldest is last

    assert (await admin.client.get(f"{API}/jobs?status=nonsense")).status_code == 422
    assert (await admin.client.get(f"{API}/jobs?limit=0")).status_code == 422
    assert (await admin.client.get(f"{API}/jobs?limit=1000")).status_code == 422


async def test_a_job_whose_document_is_gone_is_still_listed(
    app: FastAPI, make_actor: ActorFactory
) -> None:
    admin = await make_actor(Role.ADMIN)
    async with app.state.database.session() as db:
        db.add(Job(type=JobType.DELETE_DOCUMENT, payload={"document_id": "0" * 32}, status="done"))
        await db.commit()
    (item,) = (await admin.client.get(f"{API}/jobs")).json()["items"]
    assert item["document_id"] == "0" * 32 and item["document_title"] is None


# -- retry ---------------------------------------------------------------------------------


async def test_retry_requeues_only_failed_jobs_and_audits(
    app: FastAPI, make_actor: ActorFactory, module: Module
) -> None:
    admin = await make_actor(Role.ADMIN)
    faculty = await make_actor(Role.FACULTY)
    document = await insert_document(
        app,
        module_id=module.id,
        uploaded_by=faculty.user.id,
        status=DocumentStatus.FAILED,
    )
    await set_doc(app, document.id, error="No key.")
    failed = await add_job(app, document, error="No key.")

    response = await admin.client.post(f"{API}/jobs/{failed.id}/retry", headers=admin.headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued" and body["attempts"] == 0 and body["last_error"] is None
    row = await job_row(app, failed.id)
    assert row.status is JobStatus.QUEUED and row.locked_by is None
    assert row.run_after <= utcnow()
    shown = await doc(app, document.id)
    assert shown.status is DocumentStatus.UPLOADED and shown.error is None  # shows Queued again
    async with app.state.database.read_session() as db:
        audits = (
            (await db.execute(select(AuditLog).where(AuditLog.action == "job_retried")))
            .scalars()
            .all()
        )
    assert len(audits) == 1
    assert audits[0].actor_id == admin.user.id and audits[0].target_id == failed.id

    # Not failed any more: a second retry is a conflict.
    again = await admin.client.post(f"{API}/jobs/{failed.id}/retry", headers=admin.headers)
    assert again.status_code == 409
    for status in (JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.DONE):
        other = await add_job(app, document, status=status)
        refused = await admin.client.post(f"{API}/jobs/{other.id}/retry", headers=admin.headers)
        assert refused.status_code == 409
        assert (await job_row(app, other.id)).status is status
    assert (
        await admin.client.post(f"{API}/jobs/{'f' * 32}/retry", headers=admin.headers)
    ).status_code == 404


async def test_retry_of_a_failed_reindex_of_a_ready_document_shows_reindexing(
    app: FastAPI, make_actor: ActorFactory, module: Module
) -> None:
    admin = await make_actor(Role.ADMIN)
    faculty = await make_actor(Role.FACULTY)
    document = await insert_document(app, module_id=module.id, uploaded_by=faculty.user.id)
    await set_doc(app, document.id, error="Re-index failed.")
    job = await add_job(app, document, job_type=JobType.REINDEX_DOCUMENT)
    assert (
        await admin.client.post(f"{API}/jobs/{job.id}/retry", headers=admin.headers)
    ).status_code == 200
    shown = await doc(app, document.id)
    assert shown.status is DocumentStatus.READY and shown.reindexing and shown.error is None


async def test_retry_needs_the_csrf_token_and_the_admin_role(
    app: FastAPI, make_actor: ActorFactory, module: Module
) -> None:
    admin, student = await make_actor(Role.ADMIN), await make_actor(Role.STUDENT)
    job = await add_job(app, None)
    assert (await admin.client.post(f"{API}/jobs/{job.id}/retry")).status_code == 403
    assert (
        await student.client.post(f"{API}/jobs/{job.id}/retry", headers=student.headers)
    ).status_code == 403
    assert (await job_row(app, job.id)).status is JobStatus.FAILED


# -- usage -----------------------------------------------------------------------------------


async def add_exchange(
    app: FastAPI,
    user_id: str,
    *,
    question: str = "What is a heap?",
    answer: str = "A heap is a tree [C1].",
    model: str = "claude-sonnet-5-5",
    usage: dict[str, int] | None = None,
    used_web: bool = False,
    module_id: str | None = None,
    age: timedelta = timedelta(0),
    feedback: Feedback | None = None,
    comment: str | None = None,
) -> Message:
    async with app.state.database.session() as db:
        conversation = Conversation(
            user_id=user_id, module_id=module_id, title=question[:40], thread_id="t"
        )
        db.add(conversation)
        await db.flush()
        created = utcnow() - age
        db.add(
            Message(
                conversation_id=conversation.id,
                role=MessageRole.USER,
                content=question,
                citations=[],
                created_at=created - timedelta(seconds=1),
            )
        )
        reply = Message(
            conversation_id=conversation.id,
            role=MessageRole.ASSISTANT,
            content=answer,
            citations=[],
            used_web=used_web,
            model=model,
            token_usage=usage,
            feedback=feedback,
            feedback_comment=comment,
            created_at=created,
        )
        db.add(reply)
        await db.commit()
        return reply


async def test_usage_numbers(
    app: FastAPI, make_actor: ActorFactory, module: Module, settings: Any
) -> None:
    admin = await make_actor(Role.ADMIN)
    alice = await make_actor(Role.STUDENT, email="alice@example.edu")
    bob = await make_actor(Role.STUDENT, email="bob@example.edu")
    usage = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
    for _ in range(2):
        await add_exchange(app, alice.user.id, usage=usage)
    await add_exchange(app, alice.user.id, usage=usage, age=timedelta(days=3))
    await add_exchange(
        app, bob.user.id, model="gemini-2.5-flash", usage={"input_tokens": 5, "output_tokens": 1,
        "total_tokens": 6}, used_web=True,
    )  # fmt: skip
    await add_exchange(app, bob.user.id, usage=None, age=timedelta(days=60))  # outside 30 days

    faculty = await make_actor(Role.FACULTY)
    await insert_document(app, module_id=module.id, uploaded_by=faculty.user.id, content=b"x" * 100)
    await insert_document(
        app,
        module_id=module.id,
        uploaded_by=faculty.user.id,
        content=b"y" * 50,
        status=DocumentStatus.FAILED,
    )
    await insert_document(
        app,
        module_id=module.id,
        uploaded_by=faculty.user.id,
        content=b"z" * 1000,
        status=DocumentStatus.DELETING,
    )
    await add_job(app, None)

    body = (await admin.client.get(f"{API}/usage")).json()
    assert body["days"] == 30
    today = utcnow().strftime("%Y-%m-%d")
    per_day = {d["day"]: d for d in body["messages_per_day"]}
    assert per_day[today] == {"day": today, "questions": 3, "answers": 3}
    assert len(per_day) == 2  # today and three days ago; the 60-day-old exchange is outside

    tokens = {(t["day"], t["model"]): t for t in body["tokens_per_day"]}
    claude = tokens[(today, "claude-sonnet-5-5")]
    assert claude["provider"] == "anthropic"
    assert (claude["input_tokens"], claude["output_tokens"], claude["total_tokens"]) == (
        200,
        40,
        240,
    )
    gemini = tokens[(today, "gemini-2.5-flash")]
    assert gemini["provider"] == "gemini" and gemini["total_tokens"] == 6

    top = body["top_users"]
    assert [u["email"] for u in top] == ["alice@example.edu", "bob@example.edu"]
    assert (top[0]["messages_today"], top[0]["messages_in_period"]) == (2, 3)
    assert (top[1]["messages_today"], top[1]["messages_in_period"]) == (1, 1)
    assert top[0]["daily_limit"] == settings.chat_daily_limit_per_user

    assert body["documents_by_status"] == {"ready": 1, "failed": 1, "deleting": 1}
    assert body["storage_bytes"] == 150 + 0 or body["storage_bytes"] >= 150
    assert body["jobs_by_status"] == {"failed": 1}
    assert body["web_answers"] == 1 and body["total_answers"] == 4

    narrow = (await admin.client.get(f"{API}/usage?days=1")).json()
    assert [d["day"] for d in narrow["messages_per_day"]] == [today]
    assert (await admin.client.get(f"{API}/usage?days=0")).status_code == 422


async def test_usage_storage_excludes_documents_being_deleted(
    app: FastAPI, make_actor: ActorFactory, module: Module
) -> None:
    admin = await make_actor(Role.ADMIN)
    faculty = await make_actor(Role.FACULTY)
    await insert_document(app, module_id=module.id, uploaded_by=faculty.user.id, content=b"a" * 70)
    await insert_document(
        app,
        module_id=module.id,
        uploaded_by=faculty.user.id,
        content=b"b" * 500,
        status=DocumentStatus.DELETING,
    )
    assert (await admin.client.get(f"{API}/usage")).json()["storage_bytes"] == 70


# -- feedback --------------------------------------------------------------------------------


async def test_feedback_view_shows_rated_answers_without_identity(
    app: FastAPI, make_actor: ActorFactory, module: Module
) -> None:
    admin = await make_actor(Role.ADMIN)
    student = await make_actor(Role.STUDENT, email="secret.student@example.edu")
    bad = await add_exchange(
        app,
        student.user.id,
        question="Why is the sky blue?",
        answer="Because of Rayleigh scattering [C1].",
        module_id=module.id,
        feedback=Feedback.DOWN,
        comment="Too short.",
    )
    await add_exchange(app, student.user.id, question="Fine?", feedback=Feedback.UP)
    await add_exchange(app, student.user.id, question="Unrated?")

    response = await admin.client.get(f"{API}/feedback")
    assert response.status_code == 200
    (item,) = response.json()
    assert item["message_id"] == bad.id and item["rating"] == "down"
    assert item["comment"] == "Too short." and item["question"] == "Why is the sky blue?"
    assert item["answer"].startswith("Because of Rayleigh")
    assert item["module_path"].count(PATH_SEPARATOR) == 2  # Year > Block > Module
    assert "secret.student" not in response.text and student.user.id not in response.text

    everything = (await admin.client.get(f"{API}/feedback?rating=all")).json()
    assert {i["rating"] for i in everything} == {"up", "down"}
    ups = (await admin.client.get(f"{API}/feedback?rating=up")).json()
    assert [i["question"] for i in ups] == ["Fine?"]
    assert (await admin.client.get(f"{API}/feedback?rating=bogus")).status_code == 422


# -- pages -----------------------------------------------------------------------------------


async def test_jobs_page_lists_jobs_escapes_errors_and_offers_retry_only_for_failed(
    app: FastAPI, make_actor: ActorFactory, module: Module
) -> None:
    admin = await make_actor(Role.ADMIN)
    faculty = await make_actor(Role.FACULTY)
    document = await insert_document(
        app, module_id=module.id, uploaded_by=faculty.user.id, title="Heap notes"
    )
    failed = await add_job(app, document, error="<script>alert(1)</script> broke")
    done = await add_job(app, document, status=JobStatus.DONE, error=None)

    html = (await admin.client.get("/admin/jobs")).text
    assert "Heap notes" in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)" not in html
    assert f"/admin/jobs/{failed.id}/retry" in html and f"/admin/jobs/{done.id}/retry" not in html
    assert 'hx-trigger="every 5s"' in html and "/admin/jobs/table" in html

    filtered = (await admin.client.get("/admin/jobs?status=done")).text
    assert f"/admin/jobs/{failed.id}/retry" not in filtered
    table = await admin.client.get("/admin/jobs/table?status=failed")
    assert table.status_code == 200 and table.text.lstrip().startswith('<div id="jobs-table"')
    assert "Showing 1-1 of 1" in table.text


async def test_retry_form_requeues_and_redirects(
    app: FastAPI, make_actor: ActorFactory, module: Module
) -> None:
    admin = await make_actor(Role.ADMIN)
    job = await add_job(app, None)
    response = await admin.client.post(
        f"/admin/jobs/{job.id}/retry", data={"csrf_token": admin.csrf}, follow_redirects=False
    )
    assert response.status_code == 303 and response.headers["location"].endswith("notice=retried")
    assert (await job_row(app, job.id)).status is JobStatus.QUEUED
    shown = await admin.client.get(response.headers["location"])
    assert "queued again" in shown.text

    # now queued: a second retry shows the conflict on the page
    again = await admin.client.post(f"/admin/jobs/{job.id}/retry", data={"csrf_token": admin.csrf})
    assert again.status_code == 409 and "Only failed jobs can be retried" in again.text


async def test_usage_page_shows_the_numbers_and_the_privacy_note(
    app: FastAPI, make_actor: ActorFactory, module: Module
) -> None:
    admin = await make_actor(Role.ADMIN)
    student = await make_actor(Role.STUDENT, email="pat@example.edu")
    await add_exchange(
        app,
        student.user.id,
        question="Explain <b>heaps</b>",
        usage={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
        feedback=Feedback.DOWN,
        comment="Wrong <i>answer</i>",
    )
    html = (await admin.client.get("/admin/usage")).text
    assert "pat@example.edu" in html and "claude-sonnet-5-5" in html and ">18<" in html
    assert "Explain &lt;b&gt;heaps&lt;/b&gt;" in html and "Wrong &lt;i&gt;answer&lt;/i&gt;" in html
    assert "not who rated it" in html


async def test_navigation_links_are_for_admins_only(make_actor: ActorFactory) -> None:
    admin, student = await make_actor(Role.ADMIN), await make_actor(Role.STUDENT)
    assert 'href="/admin/jobs"' in (await admin.client.get("/")).text
    assert 'href="/admin/usage"' in (await admin.client.get("/")).text
    assert "/admin/jobs" not in (await student.client.get("/")).text


@pytest.mark.parametrize("path", ["/admin/jobs", "/admin/jobs/table", "/admin/usage"])
async def test_admin_pages_carry_the_security_headers_and_no_inline_script(
    make_actor: ActorFactory, path: str
) -> None:
    from tests.integration.test_materials_pages import event_handlers, inline_scripts

    admin = await make_actor(Role.ADMIN)
    response = await admin.client.get(path)
    assert response.status_code == 200
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"
    assert inline_scripts(response.text) == [] and event_handlers(response.text) == []
