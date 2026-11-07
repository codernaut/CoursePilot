from collections.abc import Awaitable, Callable
from datetime import timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from app.db.base import utcnow
from app.models import AuditLog, User, UserSession
from app.models.enums import Role
from app.services.auth import LoginThrottle
from tests.conftest import TEST_PASSWORD, Actor, api_login, insert_user, make_settings

ActorFactory = Callable[..., Awaitable[Actor]]


async def _login_attempt(client: AsyncClient, email: str, password: str) -> tuple[int, dict]:  # type: ignore[type-arg]
    token = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
        headers={"X-CSRF-Token": token},
    )
    return response.status_code, response.json()


async def test_login_success_sets_hardened_cookie(app: FastAPI, client: AsyncClient) -> None:
    await insert_user(app, email="a@example.edu", role=Role.STUDENT)
    token = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "A@Example.edu", "password": TEST_PASSWORD},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert cookie.startswith("session=")
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "path=/" in cookie
    body = response.json()
    assert body["user"]["email"] == "a@example.edu"
    assert "password_hash" not in body["user"]
    me = await client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["csrf_token"] == body["csrf_token"]


async def test_only_the_token_hash_is_stored(app: FastAPI, client: AsyncClient) -> None:
    await insert_user(app, email="a@example.edu", role=Role.STUDENT)
    await api_login(client, "a@example.edu")
    raw = client.cookies["session"]
    async with app.state.database.session() as db:
        row = (await db.execute(select(UserSession))).scalar_one()
    assert row.token_hash != raw
    assert len(row.token_hash) == 64
    assert raw not in (row.token_hash, row.csrf_token)


async def test_failed_logins_are_indistinguishable(app: FastAPI, client: AsyncClient) -> None:
    await insert_user(app, email="real@example.edu", role=Role.STUDENT)
    await insert_user(app, email="off@example.edu", role=Role.STUDENT, active=False)
    results = [
        await _login_attempt(client, "nobody@example.edu", "whatever password"),
        await _login_attempt(client, "real@example.edu", "wrong password!!"),
        await _login_attempt(client, "off@example.edu", TEST_PASSWORD),
        await _login_attempt(client, "off@example.edu", "wrong password!!"),
    ]
    assert {status for status, _ in results} == {401}
    assert len({str(body) for _, body in results}) == 1
    assert results[0][1]["error"]["code"] == "unauthorized"


async def test_inactive_user_cannot_use_existing_session(
    app: FastAPI, make_actor: ActorFactory
) -> None:
    actor = await make_actor(Role.STUDENT)
    async with app.state.database.session() as db:
        await db.execute(update(User).where(User.id == actor.user.id).values(is_active=False))
        await db.commit()
    assert (await actor.client.get("/api/v1/auth/me")).status_code == 401


async def test_login_throttled_per_ip_and_per_email(tmp_path, template_db) -> None:  # type: ignore[no-untyped-def]
    import shutil

    from app.main import create_app

    shutil.copyfile(template_db, tmp_path / "test.db")
    settings = make_settings(tmp_path, login_rate_limit_per_min=3)
    app = create_app(settings)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Origin": "http://test"},
        ) as client:
            statuses = [
                (await _login_attempt(client, f"u{i}@example.edu", "wrong password!!"))[0]
                for i in range(5)
            ]
            assert statuses[:3] == [401, 401, 401]
            assert statuses[3:] == [429, 429]  # per IP: different emails, same client address
            limited = await client.post(
                "/api/v1/auth/login",
                json={"email": "zz@example.edu", "password": "wrong password!!"},
                headers={
                    "X-CSRF-Token": (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
                },
            )
            assert limited.status_code == 429
            assert 1 <= int(limited.headers["Retry-After"]) <= 60
    finally:
        await app.state.database.dispose()


def test_throttle_retry_after_is_the_time_until_the_oldest_hit_expires() -> None:
    now = [0.0]
    throttle = LoginThrottle(2, window_s=60, clock=lambda: now[0])
    assert throttle.allow("1.1.1.1", "x@example.edu")
    now[0] = 20.0
    assert throttle.allow("1.1.1.1", "x@example.edu")
    assert not throttle.allow("1.1.1.1", "x@example.edu")
    assert throttle.retry_after("ip:1.1.1.1", "email:x@example.edu") == 40
    now[0] = 59.5
    assert throttle.retry_after("ip:1.1.1.1") == 1  # never 0
    key = "pwchange:u"
    throttle.record(key)
    throttle.record(key)
    assert throttle.is_blocked(key) and throttle.retry_after(key) == 60


def test_throttle_counts_per_email_and_expires() -> None:
    now = [0.0]
    throttle = LoginThrottle(2, window_s=60, clock=lambda: now[0])
    assert throttle.allow("1.1.1.1", "x@example.edu")
    assert throttle.allow("2.2.2.2", "x@example.edu")
    assert not throttle.allow("3.3.3.3", "x@example.edu")  # same email, other IP
    assert throttle.allow("3.3.3.3", "y@example.edu")
    now[0] = 61.0
    assert throttle.allow("3.3.3.3", "x@example.edu")  # window slid


async def test_login_rotates_session(app: FastAPI, client: AsyncClient) -> None:
    await insert_user(app, email="a@example.edu", role=Role.STUDENT)
    await api_login(client, "a@example.edu")
    first = client.cookies["session"]
    await api_login(client, "a@example.edu")  # presents the old cookie
    second = client.cookies["session"]
    assert first != second
    async with app.state.database.session() as db:
        assert len((await db.execute(select(UserSession))).scalars().all()) == 1
    stale = AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={"session": first},
    )
    async with stale:
        assert (await stale.get("/api/v1/auth/me")).status_code == 401


async def test_logout_invalidates_session(app: FastAPI, make_actor: ActorFactory) -> None:
    actor = await make_actor(Role.STUDENT)
    stolen = actor.client.cookies["session"]
    response = await actor.client.post("/api/v1/auth/logout", headers=actor.headers)
    assert response.status_code == 200
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", cookies={"session": stolen}
    ) as replay:
        assert (await replay.get("/api/v1/auth/me")).status_code == 401


async def test_expired_session_is_rejected(app: FastAPI, make_actor: ActorFactory) -> None:
    actor = await make_actor(Role.STUDENT)
    async with app.state.database.session() as db:
        await db.execute(update(UserSession).values(expires_at=utcnow() - timedelta(seconds=1)))
        await db.commit()
    assert (await actor.client.get("/api/v1/auth/me")).status_code == 401


async def test_must_change_password_gating(app: FastAPI, make_actor: ActorFactory) -> None:
    actor = await make_actor(Role.STUDENT, must_change_password=True)
    me = await actor.client.get("/api/v1/auth/me")
    assert me.status_code == 403
    assert me.json()["error"]["code"] == "password_change_required"
    assert (await actor.client.get("/api/v1/structure")).status_code == 403
    page = await actor.client.get("/", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"] == "/change-password"
    allowed = await actor.client.get("/change-password")
    assert allowed.status_code == 200
    changed = await actor.client.post(
        "/api/v1/auth/change-password",
        json={"current_password": TEST_PASSWORD, "new_password": "a brand new password"},
        headers=actor.headers,
    )
    assert changed.status_code == 200
    assert (await actor.client.get("/api/v1/auth/me")).status_code == 200


async def test_change_password_revokes_other_sessions(
    app: FastAPI, make_actor: ActorFactory
) -> None:
    first = await make_actor(Role.STUDENT, email="s@example.edu")
    second_client = AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Origin": "http://test"},
    )
    async with second_client:
        second_csrf = await api_login(second_client, "s@example.edu")
        response = await second_client.post(
            "/api/v1/auth/change-password",
            json={"current_password": TEST_PASSWORD, "new_password": "another good password"},
            headers={"X-CSRF-Token": second_csrf},
        )
        assert response.status_code == 200
        assert (await second_client.get("/api/v1/auth/me")).status_code == 200
        assert (await first.client.get("/api/v1/auth/me")).status_code == 401
    token = (await first.client.get("/api/v1/auth/csrf")).json()["csrf_token"]
    old = await first.client.post(
        "/api/v1/auth/login",
        json={"email": "s@example.edu", "password": TEST_PASSWORD},
        headers={"X-CSRF-Token": token},
    )
    assert old.status_code == 401


@pytest.mark.parametrize(
    ("current", "new", "status"),
    [
        ("wrong current pw", "a brand new password", 400),
        (TEST_PASSWORD, "short", 422),
        (TEST_PASSWORD, TEST_PASSWORD, 422),
    ],
)
async def test_change_password_validation(
    make_actor: ActorFactory, current: str, new: str, status: int
) -> None:
    actor = await make_actor(Role.STUDENT)
    response = await actor.client.post(
        "/api/v1/auth/change-password",
        json={"current_password": current, "new_password": new},
        headers=actor.headers,
    )
    assert response.status_code == status


async def test_login_and_logout_are_audited(app: FastAPI, client: AsyncClient) -> None:
    await insert_user(app, email="a@example.edu", role=Role.STUDENT)
    await _login_attempt(client, "a@example.edu", "wrong password!!")
    csrf = await api_login(client, "a@example.edu")
    await client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})
    async with app.state.database.session() as db:
        actions = [r.action for r in (await db.execute(select(AuditLog))).scalars()]
    assert {"login_failed", "login_succeeded", "logout"} <= set(actions)


async def test_failed_login_audit_does_not_store_password(
    app: FastAPI, client: AsyncClient
) -> None:
    await _login_attempt(client, "x@example.edu", "super secret guess")
    async with app.state.database.session() as db:
        rows = (await db.execute(select(AuditLog))).scalars().all()
    assert rows
    assert all("super secret guess" not in str(r.details) for r in rows)
