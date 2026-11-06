"""ALLOW_INSECURE_HTTP: cookie, HSTS, banner, startup warning (CLAUDE.md section 13)."""

import asyncio
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core import startup
from app.core.config import Settings
from app.main import create_app
from app.models.enums import Role
from app.worker import run_worker
from tests.conftest import TEST_PASSWORD, Actor, insert_user, make_settings

ActorFactory = Callable[..., Awaitable[Actor]]
LAN_URL = "http://192.168.1.20:8000"
BANNER = "Traffic to this server is not encrypted"


@pytest.fixture
def settings(tmp_path: Path, template_db: Path) -> Settings:
    """Module override: every app in this file runs with the flag on (test environment)."""
    shutil.copyfile(template_db, tmp_path / "test.db")
    return make_settings(tmp_path, allow_insecure_http=True)


@asynccontextmanager
async def production_app(
    tmp_path: Path, template_db: Path, *, insecure: bool
) -> AsyncIterator[tuple[FastAPI, AsyncClient]]:
    shutil.copyfile(template_db, tmp_path / "test.db")
    base = LAN_URL if insecure else "https://example.edu"
    settings = make_settings(
        tmp_path,
        app_env="production",
        base_url=base,
        secret_key="p" * 40,
        allow_insecure_http=insecure,
    )
    application = create_app(settings)
    try:
        await insert_user(application, email="prod@example.edu", role=Role.STUDENT)
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url=base, headers={"Origin": base}
        ) as c:
            yield application, c
    finally:
        await application.state.database.dispose()


async def _login(c: AsyncClient) -> Any:
    token = (await c.get("/api/v1/auth/csrf")).json()["csrf_token"]
    return await c.post(
        "/api/v1/auth/login",
        json={"email": "prod@example.edu", "password": TEST_PASSWORD},
        headers={"X-CSRF-Token": token},
    )


async def test_insecure_production_cookie_is_plain_session_cookie(
    tmp_path: Path, template_db: Path
) -> None:
    async with production_app(tmp_path, template_db, insecure=True) as (_, c):
        response = await _login(c)
        assert response.status_code == 200
        cookie = response.headers["set-cookie"].lower()
        assert cookie.startswith("session=")
        assert "__host-" not in cookie
        assert "secure" not in cookie  # would never be sent back over http
        assert "httponly" in cookie and "samesite=lax" in cookie and "path=/" in cookie
        # The browser can send it back over plain http: the session works.
        assert (await c.get("/api/v1/auth/me")).status_code == 200


async def test_secure_production_cookie_is_unchanged(tmp_path: Path, template_db: Path) -> None:
    async with production_app(tmp_path, template_db, insecure=False) as (_, c):
        cookie = (await _login(c)).headers["set-cookie"].lower()
        assert cookie.startswith("__host-session=")
        assert "secure" in cookie and "httponly" in cookie


async def test_logout_clears_the_matching_cookie_in_each_mode(
    tmp_path: Path, template_db: Path
) -> None:
    async with production_app(tmp_path, template_db, insecure=True) as (_, c):
        csrf = (await _login(c)).json()["csrf_token"]
        out = await c.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})
        assert out.headers["set-cookie"].lower().startswith("session=")
        assert "secure" not in out.headers["set-cookie"].lower()


@pytest.mark.parametrize(("insecure", "hsts"), [(False, True), (True, False)])
async def test_hsts_only_when_not_insecure(
    tmp_path: Path, template_db: Path, insecure: bool, hsts: bool
) -> None:
    async with production_app(tmp_path, template_db, insecure=insecure) as (_, c):
        headers = (await c.get("/login")).headers
        assert ("strict-transport-security" in headers) is hsts
        # Every other security header stays on in both modes.
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        assert headers["x-frame-options"] == "DENY"
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["referrer-policy"] == "same-origin"
        assert headers["cache-control"] == "no-store"


async def test_insecure_production_keeps_csrf_origin_and_docs_off(
    tmp_path: Path, template_db: Path
) -> None:
    async with production_app(tmp_path, template_db, insecure=True) as (_, c):
        # No login CSRF token -> rejected.
        bad = await c.post("/api/v1/auth/login", json={"email": "a@b.co", "password": "x" * 12})
        assert bad.status_code == 403
        # Foreign Origin -> rejected even with a valid token.
        token = (await c.get("/api/v1/auth/csrf")).json()["csrf_token"]
        foreign = await c.post(
            "/api/v1/auth/login",
            json={"email": "prod@example.edu", "password": TEST_PASSWORD},
            headers={"X-CSRF-Token": token, "Origin": "http://evil.example"},
        )
        assert foreign.status_code == 403
        # Session-authenticated POST without the session CSRF token -> rejected.
        await _login(c)
        assert (await c.post("/api/v1/auth/logout")).status_code == 403
        # Production: no API docs.
        assert (await c.get("/docs")).status_code == 404
        assert (await c.get("/api/v1/openapi.json")).status_code == 404


async def test_banner_is_shown_to_admins_only(make_actor: ActorFactory, seeded: None) -> None:
    admin = await make_actor(Role.ADMIN)
    student = await make_actor(Role.STUDENT)
    faculty = await make_actor(Role.FACULTY)
    for path in ("/", "/structure", "/admin/users", "/change-password"):
        page = await admin.client.get(path)
        assert BANNER in page.text and "ALLOW_INSECURE_HTTP=true" in page.text, path
    for other in (student, faculty):
        assert BANNER not in (await other.client.get("/")).text


async def test_banner_is_not_shown_to_anonymous_visitors(client: AsyncClient) -> None:
    assert BANNER not in (await client.get("/login")).text


class _RecordingLog:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **kw: Any) -> None:
        self.warnings.append((event, kw))


def _prod_settings(tmp_path: Path, *, insecure: bool) -> Settings:
    return make_settings(
        tmp_path,
        app_env="production",
        base_url=LAN_URL if insecure else "https://example.edu",
        secret_key="p" * 40,
        allow_insecure_http=insecure,
    )


async def test_app_logs_warning_at_startup_when_on_in_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _RecordingLog()
    monkeypatch.setattr(startup, "log", recorder)
    application = create_app(_prod_settings(tmp_path, insecure=True))
    await application.state.database.dispose()
    assert [event for event, _ in recorder.warnings] == ["insecure_http_enabled"]


async def test_app_logs_no_warning_when_off_or_not_production(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _RecordingLog()
    monkeypatch.setattr(startup, "log", recorder)
    secure = create_app(_prod_settings(tmp_path, insecure=False))
    await secure.state.database.dispose()
    development = create_app(settings)  # flag on, but APP_ENV=test: nothing to warn about
    await development.state.database.dispose()
    assert recorder.warnings == []


async def test_worker_logs_warning_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _RecordingLog()
    monkeypatch.setattr(startup, "log", recorder)
    stop = asyncio.Event()
    stop.set()  # return right after startup
    await run_worker(_prod_settings(tmp_path, insecure=True), stop)
    assert [event for event, _ in recorder.warnings] == ["insecure_http_enabled"]
    recorder.warnings.clear()
    await run_worker(_prod_settings(tmp_path, insecure=False), stop)
    assert recorder.warnings == []
