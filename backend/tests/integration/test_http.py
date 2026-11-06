import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.models.enums import Role
from tests.conftest import TEST_PASSWORD, Actor, insert_user, make_settings

ActorFactory = Callable[..., Awaitable[Actor]]


async def test_health_ready_ok(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok", "sqlite_vec": "ok"}}


async def test_health_is_public(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/health/live")).status_code == 200
    assert (await client.get("/api/v1/health/ready")).status_code == 200


async def test_health_ready_fails_with_error_shape(
    app: FastAPI, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(app.state.database, "read_session", broken)
    response = await client.get("/api/v1/health/ready")
    assert response.status_code == 503
    body = response.json()["error"]
    assert body["code"] == "not_ready"
    assert "db down" not in response.text


@pytest.mark.parametrize("path", ["/login", "/api/v1/health/live", "/static/css/app.css", "/nope"])
async def test_security_headers_on_every_response(client: AsyncClient, path: str) -> None:
    response = await client.get(path)
    headers = response.headers
    assert headers["content-security-policy"] == (
        "default-src 'self'; script-src 'self'; object-src 'none'; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'"
    )
    assert headers["x-frame-options"] == "DENY"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "same-origin"
    assert "strict-transport-security" not in headers  # not production


async def test_pages_and_api_are_not_cached(client: AsyncClient) -> None:
    assert (await client.get("/login")).headers["cache-control"] == "no-store"
    assert (await client.get("/api/v1/health/live")).headers["cache-control"] == "no-store"
    assert "no-store" not in (await client.get("/static/css/app.css")).headers.get(
        "cache-control", ""
    )


async def test_docs_have_relaxed_csp_only_there(make_actor: ActorFactory) -> None:
    actor = await make_actor(Role.STUDENT)
    docs = await actor.client.get("/docs")
    assert docs.status_code == 200
    assert "cdn.jsdelivr.net" in docs.headers["content-security-policy"]
    home = await actor.client.get("/")
    assert "cdn.jsdelivr.net" not in home.headers["content-security-policy"]


async def test_no_inline_scripts_or_external_assets_in_pages(
    seeded: None, make_actor: ActorFactory
) -> None:
    admin = await make_actor(Role.ADMIN)
    for path in ("/", "/structure", "/admin/users", "/change-password"):
        html = (await admin.client.get(path)).text
        assert "http://" not in html and "https://" not in html, path
        scripts = html.split("<script")[1:]
        assert all(s.lstrip().startswith("src=") for s in scripts), path


async def test_errors_use_one_json_shape(client: AsyncClient) -> None:
    for response in (
        await client.get("/api/v1/nope"),
        await client.get("/api/v1/users"),
        await client.post("/api/v1/auth/login", json={"email": "x"}),
    ):
        body = response.json()
        assert set(body) == {"error"}
        assert set(body["error"]) == {"code", "message", "details"}


async def test_production_cookie_is_host_prefixed_and_secure(
    tmp_path: Path, template_db: Path
) -> None:
    shutil.copyfile(template_db, tmp_path / "test.db")
    settings = make_settings(
        tmp_path, app_env="production", base_url="https://example.edu", secret_key="p" * 40
    )
    application = create_app(settings)
    try:
        await insert_user(application, email="prod@example.edu", role=Role.STUDENT)
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="https://example.edu",
            headers={"Origin": "https://example.edu"},
        ) as c:
            token = (await c.get("/api/v1/auth/csrf")).json()["csrf_token"]
            response = await c.post(
                "/api/v1/auth/login",
                json={"email": "prod@example.edu", "password": TEST_PASSWORD},
                headers={"X-CSRF-Token": token},
            )
            assert response.status_code == 200
            cookie = response.headers["set-cookie"].lower()
            assert cookie.startswith("__host-session=")
            assert "secure" in cookie and "httponly" in cookie and "path=/" in cookie
            assert "domain=" not in cookie
    finally:
        await application.state.database.dispose()


async def _failed_logins(
    client: AsyncClient, count: int, xff: list[str] | None = None
) -> list[int]:
    statuses = []
    for i in range(count):
        token = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
        headers = {"X-CSRF-Token": token}
        if xff:
            headers["X-Forwarded-For"] = xff[i]
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": f"u{i}@example.edu", "password": "wrong password!!"},
            headers=headers,
        )
        statuses.append(response.status_code)
    return statuses


@pytest.mark.parametrize(
    ("behind_proxy", "expected_last"),
    [
        (False, 429),  # X-Forwarded-For is attacker-controlled: ignored, the socket IP counts
        (True, 429),  # trusted proxy: its appended (last) entry is the client, same for all
    ],
)
async def test_throttle_ignores_spoofed_forwarded_for(
    tmp_path: Path, template_db: Path, behind_proxy: bool, expected_last: int
) -> None:
    shutil.copyfile(template_db, tmp_path / "test.db")
    settings = make_settings(tmp_path, login_rate_limit_per_min=3, behind_proxy=behind_proxy)
    application = create_app(settings)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
            headers={"Origin": "http://test"},
        ) as c:
            # The first entry is spoofed per request; the last is the proxy-appended real client.
            xff = [f"10.0.0.{i}, 203.0.113.9" for i in range(4)]
            statuses = await _failed_logins(c, 4, xff)
        assert statuses == [401, 401, 401, expected_last]
    finally:
        await application.state.database.dispose()


async def test_throttle_distinguishes_clients_behind_proxy(
    tmp_path: Path, template_db: Path
) -> None:
    shutil.copyfile(template_db, tmp_path / "test.db")
    settings = make_settings(tmp_path, login_rate_limit_per_min=3, behind_proxy=True)
    application = create_app(settings)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
            headers={"Origin": "http://test"},
        ) as c:
            statuses = await _failed_logins(c, 4, [f"203.0.113.{i}" for i in range(4)])
        assert statuses == [401, 401, 401, 401]  # four different real clients, none limited
    finally:
        await application.state.database.dispose()


async def test_email_throttle_is_independent_of_ip(tmp_path: Path, template_db: Path) -> None:
    shutil.copyfile(template_db, tmp_path / "test.db")
    settings = make_settings(tmp_path, login_rate_limit_per_min=3, behind_proxy=True)
    application = create_app(settings)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
            headers={"Origin": "http://test"},
        ) as c:
            statuses = []
            for i in range(4):
                token = (await c.get("/api/v1/auth/csrf")).json()["csrf_token"]
                response = await c.post(
                    "/api/v1/auth/login",
                    json={"email": "victim@example.edu", "password": "wrong password!!"},
                    headers={"X-CSRF-Token": token, "X-Forwarded-For": f"198.51.100.{i}"},
                )
                statuses.append(response.status_code)
        assert statuses == [401, 401, 401, 429]
    finally:
        await application.state.database.dispose()
