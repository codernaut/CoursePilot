"""Structured logging: one access-log line per request, worker lines carry job context, and nothing
sensitive (prompts, answers, passwords, tokens, keys, emails) reaches the log."""

import asyncio
import io
import json
from collections.abc import Awaitable, Callable, Iterator

import pytest
import structlog
from fastapi import FastAPI
from httpx import AsyncClient

from app.core.logging import configure_logging
from app.ingestion.handlers import build_failure_hook, build_handlers
from app.jobs import JobRunner, SqliteJobQueue
from app.models.enums import JobType, Role
from tests.conftest import TEST_PASSWORD, Actor
from tests.ingest_support import Env

ActorFactory = Callable[..., Awaitable[Actor]]


@pytest.fixture
def log() -> Iterator[io.StringIO]:
    buffer = io.StringIO()
    configure_logging("INFO", json=True, stream=buffer)
    yield buffer
    configure_logging("INFO", json=True)


def entries(buffer: io.StringIO, event: str | None = None) -> list[dict[str, object]]:
    rows = [json.loads(line) for line in buffer.getvalue().splitlines() if line.startswith("{")]
    return [r for r in rows if event is None or r["event"] == event]


async def test_every_request_gets_one_line_with_the_expected_fields(
    make_actor: ActorFactory, log: io.StringIO
) -> None:
    student = await make_actor(Role.STUDENT, email="private.person@example.edu")
    log.truncate(0)
    log.seek(0)
    response = await student.client.get("/api/v1/auth/me", headers={"X-Request-ID": "req-abc-123"})
    assert response.status_code == 200
    (line,) = entries(log, "http_request")
    assert line["request_id"] == "req-abc-123" == response.headers["x-request-id"]
    assert line["user_id"] == student.user.id
    assert line["method"] == "GET" and line["route"] == "/api/v1/auth/me" and line["status"] == 200
    assert isinstance(line["latency_ms"], int | float) and line["latency_ms"] >= 0
    assert line["level"] == "info" and "timestamp" in line
    assert "private.person" not in log.getvalue()


async def test_ids_in_paths_are_grouped_and_query_strings_are_never_logged(
    make_actor: ActorFactory, log: io.StringIO
) -> None:
    admin = await make_actor(Role.ADMIN)
    log.truncate(0)
    log.seek(0)
    await admin.client.get(f"/api/v1/users/{'a' * 32}")
    await admin.client.get("/api/v1/admin/jobs?status=failed&secret=hunter2hunter2")
    routes = [e["route"] for e in entries(log, "http_request")]
    assert routes == ["/api/v1/users/{id}", "/api/v1/admin/jobs"]
    assert "hunter2" not in log.getvalue() and "status=failed" not in log.getvalue()


async def test_anonymous_failed_and_unknown_requests_are_logged_too(
    make_actor: ActorFactory,
    client: AsyncClient,
    log: io.StringIO,
) -> None:
    assert (await client.get("/api/v1/users")).status_code == 401
    assert (await client.get("/api/v1/nothing-here")).status_code == 404
    lines = entries(log, "http_request")
    assert [(e["status"], e["user_id"]) for e in lines] == [(401, None), (404, None)]


async def test_login_does_not_log_the_password_or_the_session_cookie(
    make_actor: ActorFactory,
    client: AsyncClient,
    log: io.StringIO,
) -> None:
    await make_actor(Role.STUDENT, email="login.user@example.edu")
    log.truncate(0)
    log.seek(0)
    token = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "login.user@example.edu", "password": TEST_PASSWORD},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 200
    text = log.getvalue()
    assert TEST_PASSWORD not in text and token not in text
    assert response.json()["csrf_token"] not in text
    for cookie in client.cookies.jar:
        assert cookie.value is None or cookie.value not in text
    assert "login.user@example.edu" not in text


async def test_worker_lines_carry_job_id_type_and_document_id(
    ingest_env: Env, log: io.StringIO
) -> None:
    env = ingest_env
    doc = await env.add_document(
        b"# Heaps\n\nA heap keeps the largest item at the root.\n",
        "heaps.md",
        job=JobType.INGEST_DOCUMENT,
    )
    queue = SqliteJobQueue(env.database, lock_timeout_s=600)
    runner = JobRunner(
        queue, build_handlers(env.deps), "w", on_failure=build_failure_hook(env.deps)
    )
    assert await runner.run_next(asyncio.Event())

    rows = entries(log)
    events = {r["event"]: r for r in rows}
    for name in ("job_started", "document_processed", "job_done"):
        row = events[name]
        assert row["job_type"] == "ingest_document" and row["document_id"] == doc.id
        assert isinstance(row["job_id"], str) and len(str(row["job_id"])) == 32
    assert structlog.contextvars.get_contextvars() == {}  # the context does not leak


async def test_a_failing_job_logs_a_readable_error_without_document_text(
    ingest_env: Env, log: io.StringIO
) -> None:
    env = ingest_env
    await env.add_document(b"%PDF- not really", "bad.pdf", job=JobType.INGEST_DOCUMENT)
    queue = SqliteJobQueue(env.database, lock_timeout_s=600)
    runner = JobRunner(
        queue, build_handlers(env.deps), "w", on_failure=build_failure_hook(env.deps)
    )
    await runner.run_next(asyncio.Event())
    (failed,) = entries(log, "job_failed")
    assert failed["job_type"] == "ingest_document" and failed["outcome"] == "failed"
    assert "not really" not in log.getvalue()


async def test_a_crashing_request_is_logged_as_500_without_the_sql_parameters(
    app: FastAPI, client: AsyncClient, log: io.StringIO
) -> None:
    from sqlalchemy.exc import IntegrityError

    async def crash() -> None:
        value = "secret" + " chat text"  # built here so the source line does not contain it
        raise IntegrityError("INSERT INTO t VALUES (?)", (value,), Exception("bad"))

    app.add_api_route("/crash-test", crash)
    response = await client.get("/crash-test?q=private+query")
    assert response.status_code == 500 and response.headers["x-request-id"]
    (line,) = entries(log, "http_request")
    assert line["status"] == 500 and line["request_id"] == response.headers["x-request-id"]
    assert "secret chat text" not in log.getvalue() and "private+query" not in log.getvalue()
