"""Regression tests for bugs found during the adversarial review of M1."""

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.models import User
from app.models.enums import Role
from app.services import auth
from tests.conftest import Actor, insert_user

ActorFactory = Callable[..., Awaitable[Actor]]


@pytest.mark.parametrize(
    "origin",
    [
        "http://test:abc",  # non-numeric port
        "http://test:99999",  # out-of-range port
        "http://[::1",  # malformed IPv6 literal
    ],
)
async def test_malformed_origin_is_rejected_not_a_server_error(
    client: AsyncClient, origin: str
) -> None:
    token = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "a@example.edu", "password": "x" * 12},
        headers={"X-CSRF-Token": token, "Origin": origin},
    )
    assert response.status_code == 403


async def test_origin_with_userinfo_is_rejected(client: AsyncClient) -> None:
    token = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "a@example.edu", "password": "x" * 12},
        headers={"X-CSRF-Token": token, "Origin": "http://evil.test@test"},
    )
    assert response.status_code == 403


async def test_unhandled_errors_still_carry_security_headers(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_: object, **__: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(auth, "resolve_session", boom)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        ac.cookies.set("session", "x")
        response = await ac.get("/api/v1/auth/me")
    assert response.status_code == 500
    assert response.headers["x-request-id"]
    assert "content-security-policy" in response.headers
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"


async def test_concurrent_admin_deactivations_never_remove_every_admin(
    app: FastAPI, make_actor: ActorFactory
) -> None:
    a = await make_actor(Role.ADMIN)
    b = await make_actor(Role.ADMIN)
    results = await asyncio.gather(
        a.client.patch(f"/api/v1/users/{b.user.id}", json={"is_active": False}, headers=a.headers),
        b.client.patch(f"/api/v1/users/{a.user.id}", json={"is_active": False}, headers=b.headers),
    )
    assert any(r.status_code == 200 for r in results)
    async with app.state.database.session() as db:
        assert await db.scalar(select(User.id).where(User.role == Role.ADMIN, User.is_active))


async def test_session_of_a_deactivated_admin_cannot_act_in_flight(
    app: FastAPI, make_actor: ActorFactory
) -> None:
    """A user deactivated by someone else mid-request must not complete an admin action."""
    a = await make_actor(Role.ADMIN)
    b = await make_actor(Role.ADMIN)
    victim = await insert_user(app, email="v@example.edu", role=Role.STUDENT)
    # b is deactivated first; b's already-issued session must not be accepted afterwards.
    r = await a.client.patch(
        f"/api/v1/users/{b.user.id}", json={"is_active": False}, headers=a.headers
    )
    assert r.status_code == 200
    r = await b.client.patch(
        f"/api/v1/users/{victim.id}", json={"is_active": False}, headers=b.headers
    )
    assert r.status_code == 401


async def test_pre_login_token_is_not_a_session_csrf_token(
    client: AsyncClient, make_actor: ActorFactory
) -> None:
    actor = await make_actor(Role.STUDENT)
    pre_login = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
    response = await actor.client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "x", "new_password": "y" * 12},
        headers={"X-CSRF-Token": pre_login},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_csrf_token_of_another_session_is_rejected(make_actor: ActorFactory) -> None:
    first = await make_actor(Role.ADMIN)
    second = await make_actor(Role.ADMIN)
    response = await first.client.patch(
        f"/api/v1/users/{second.user.id}",
        json={"display_name": "Hacked"},
        headers=second.headers,  # a valid token, but for someone else's session
    )
    assert response.status_code == 403


async def test_htmx_does_not_inject_inline_styles_that_the_csp_blocks(
    make_actor: ActorFactory,
) -> None:
    actor = await make_actor(Role.STUDENT)
    html = (await actor.client.get("/")).text
    assert '"includeIndicatorStyles": false' in html
    assert '"allowEval": false' in html
