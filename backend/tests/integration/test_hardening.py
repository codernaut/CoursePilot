"""Password policy at every entry point, change-password throttle, audit retention, body limit,
version info."""

import re
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app import __version__
from app.core.config import Settings
from app.core.version import get_commit
from app.db.base import utcnow
from app.models import AuditLog
from app.models.enums import Role
from app.services import audit
from app.worker import purge_audit_once
from tests.conftest import TEST_PASSWORD, Actor

ActorFactory = Callable[..., Awaitable[Actor]]
CHANGE = "/api/v1/auth/change-password"


# --- password policy -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("new", "fragment"),
    [
        ("password123", "too common"),
        ("          ", "only spaces"),
        ("s1@example.edu", "email"),
        ("S1", "at least 10"),
    ],
)
async def test_change_password_applies_policy(
    make_actor: ActorFactory, new: str, fragment: str
) -> None:
    actor = await make_actor(Role.STUDENT, email="s1@example.edu")
    response = await actor.client.post(
        CHANGE,
        json={"current_password": TEST_PASSWORD, "new_password": new},
        headers=actor.headers,
    )
    assert response.status_code == 422
    assert fragment in response.json()["error"]["message"]


async def test_change_password_rejects_local_part_of_email(make_actor: ActorFactory) -> None:
    actor = await make_actor(Role.STUDENT, email="long.student.name@example.edu")
    response = await actor.client.post(
        CHANGE,
        json={"current_password": TEST_PASSWORD, "new_password": "Long.Student.Name"},
        headers=actor.headers,
    )
    assert response.status_code == 422
    assert "email" in response.json()["error"]["message"]


async def test_admin_create_user_applies_policy(make_actor: ActorFactory) -> None:
    admin = await make_actor(Role.ADMIN)
    base = {"display_name": "N", "role": "student"}
    common = await admin.client.post(
        "/api/v1/users",
        json={**base, "email": "n@example.edu", "password": "Password123"},
        headers=admin.headers,
    )
    assert common.status_code == 422 and "too common" in common.json()["error"]["message"]
    same = await admin.client.post(
        "/api/v1/users",
        json={**base, "email": "new.person@example.edu", "password": "new.person"},
        headers=admin.headers,
    )
    assert same.status_code == 422 and "email" in same.json()["error"]["message"]
    generated = await admin.client.post(
        "/api/v1/users", json={**base, "email": "g@example.edu"}, headers=admin.headers
    )
    assert generated.status_code == 201  # temporary passwords comply


async def test_create_initial_admin_applies_policy(app: FastAPI) -> None:
    from app.core.errors import AppError
    from app.services import users

    async with app.state.database.session() as db:
        with pytest.raises(AppError, match="too common"):
            await users.create_initial_admin(
                db, email="root@example.edu", display_name="R", password="qwertyuiop"
            )
        with pytest.raises(AppError, match="email"):
            await users.create_initial_admin(
                db, email="root@example.edu", display_name="R", password="root@example.edu"
            )
        await users.create_initial_admin(
            db, email="root@example.edu", display_name="R", password="a fine passphrase"
        )


# --- change-password throttle ----------------------------------------------------------------


async def test_change_password_wrong_current_is_throttled_per_user(tmp_path_factory, template_db):  # type: ignore[no-untyped-def]
    import shutil

    from app.main import create_app
    from tests.conftest import api_login, insert_user, make_settings

    folder = tmp_path_factory.mktemp("throttle")
    shutil.copyfile(template_db, folder / "test.db")
    application = create_app(make_settings(folder, login_rate_limit_per_min=3))
    try:
        user = await insert_user(application, email="t@example.edu", role=Role.STUDENT)
        other = await insert_user(application, email="o@example.edu", role=Role.STUDENT)
        async with (
            AsyncClient(
                transport=ASGITransport(app=application),
                base_url="http://test",
                headers={"Origin": "http://test"},
            ) as c,
            AsyncClient(
                transport=ASGITransport(app=application),
                base_url="http://test",
                headers={"Origin": "http://test"},
            ) as c2,
        ):
            csrf = await api_login(c, user.email)
            csrf2 = await api_login(c2, other.email)

            async def attempt(client: AsyncClient, token: str, current: str) -> int:
                response = await client.post(
                    CHANGE,
                    json={"current_password": current, "new_password": "a brand new password"},
                    headers={"X-CSRF-Token": token},
                )
                return response.status_code

            statuses = [await attempt(c, csrf, "wrong password!!") for _ in range(4)]
            assert statuses == [400, 400, 400, 429]
            # Even the right password is refused while limited ...
            assert await attempt(c, csrf, TEST_PASSWORD) == 429
            # ... and another user is not affected.
            assert await attempt(c2, csrf2, TEST_PASSWORD) == 200
    finally:
        await application.state.database.dispose()


async def test_successful_change_resets_the_failure_count(make_actor: ActorFactory) -> None:
    actor = await make_actor(Role.STUDENT)

    async def attempt(current: str, new: str) -> int:
        response = await actor.client.post(
            CHANGE,
            json={"current_password": current, "new_password": new},
            headers=actor.headers,
        )
        return response.status_code

    for _ in range(5):  # limit in tests is 50: stays well below it
        assert await attempt("wrong password!!", "a brand new password") == 400
    assert await attempt(TEST_PASSWORD, "a brand new password") == 200


# --- audit retention -------------------------------------------------------------------------


async def test_audit_purge_applies_both_retention_windows(app: FastAPI, settings: Settings) -> None:
    now = utcnow()
    rows = [
        ("login_succeeded", 10, True),  # recent: kept
        ("login_succeeded", 400, False),  # older than 365 days: purged
        ("password_changed", 364, True),  # just inside the window: kept
        ("login_failed", 5, True),  # recent failure: kept
        ("login_failed", 31, False),  # failure past 30 days: purged although < 365
        ("login_failed", 500, False),  # purged
    ]
    async with app.state.database.session() as db:
        for action, age_days, _ in rows:
            db.add(AuditLog(actor_id=None, action=action, details={"age": age_days}))
        await db.commit()
        entries = list((await db.execute(select(AuditLog))).scalars())
        for entry in entries:
            entry.created_at = now - timedelta(days=entry.details["age"])
        await db.commit()

    assert await purge_audit_once(app.state.database, settings) == 3
    async with app.state.database.read_session() as db:
        kept = sorted(
            (r.action, r.details["age"]) for r in (await db.execute(select(AuditLog))).scalars()
        )
    assert kept == sorted((a, d) for a, d, keep in rows if keep)


async def test_audit_purge_honours_custom_windows(app: FastAPI) -> None:
    now = utcnow()
    async with app.state.database.session() as db:
        db.add(AuditLog(actor_id=None, action="logout", details={}))
        await db.commit()
        row = (await db.execute(select(AuditLog))).scalar_one()
        row.created_at = now - timedelta(days=3)
        await db.commit()
        assert (
            await audit.purge_old(db, retention_days=7, login_failed_retention_days=1, now=now) == 0
        )
        assert (
            await audit.purge_old(db, retention_days=2, login_failed_retention_days=1, now=now) == 1
        )


# --- request body limit ----------------------------------------------------------------------


async def test_oversized_body_is_rejected_with_standard_413(client: AsyncClient) -> None:
    big = b"x" * (2 * 1024 * 1024 + 1)
    response = await client.post(
        "/api/v1/auth/login", content=big, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    body = response.json()
    assert body["error"]["code"] == "payload_too_large"
    assert set(body["error"]) == {"code", "message", "details"}
    assert "x-request-id" in response.headers
    assert response.headers["x-content-type-options"] == "nosniff"  # security headers still apply


async def test_body_at_the_limit_is_not_rejected_by_the_middleware(client: AsyncClient) -> None:
    ok = await client.post(
        "/api/v1/auth/login",
        content=b"{" + b" " * (2 * 1024 * 1024 - 2) + b"}",
        headers={"Content-Type": "application/json"},
    )
    assert ok.status_code != 413


async def test_streamed_body_without_content_length_is_limited(client: AsyncClient) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(3):
            yield b"y" * (1024 * 1024)

    response = await client.post(
        "/api/v1/auth/login", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


async def test_exempt_path_skips_the_limit() -> None:
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    from app.core.middleware import BodySizeLimitMiddleware

    async def sink(request):  # type: ignore[no-untyped-def]
        return PlainTextResponse(str(len(await request.body())))

    app = Starlette(
        routes=[Route("/up", sink, methods=["POST"]), Route("/other", sink, methods=["POST"])]
    )
    wrapped = BodySizeLimitMiddleware(app, max_bytes=100, exempt=[re.compile(r"^/up$")])
    async with AsyncClient(transport=ASGITransport(app=wrapped), base_url="http://t") as tc:
        assert (await tc.post("/up", content=b"z" * 5000)).status_code == 200
        assert (await tc.post("/other", content=b"z" * 5000)).status_code == 413
        assert (await tc.post("/other", content=b"z" * 50)).status_code == 200


# --- version info ----------------------------------------------------------------------------


async def test_health_version_is_public_and_matches_pyproject(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health/version")
    assert response.status_code == 200
    assert response.json() == {"version": __version__, "commit": get_commit()}


async def test_footer_shows_version_and_commit(
    make_actor: ActorFactory, client: AsyncClient
) -> None:
    label = f"v{__version__} · {get_commit()}"
    assert label in (await client.get("/login")).text
    actor = await make_actor(Role.STUDENT)
    assert label in (await actor.client.get("/")).text
