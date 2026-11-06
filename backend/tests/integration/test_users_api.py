from collections.abc import Awaitable, Callable

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.models import AuditLog, User, UserSession
from app.models.enums import Role
from tests.conftest import TEST_PASSWORD, Actor, api_login

ActorFactory = Callable[..., Awaitable[Actor]]


async def _audit(app: FastAPI, action: str) -> list[AuditLog]:
    async with app.state.database.session() as db:
        rows = (await db.execute(select(AuditLog).where(AuditLog.action == action))).scalars()
        return list(rows)


async def test_create_user_generates_temporary_password(
    app: FastAPI, make_actor: ActorFactory
) -> None:
    admin = await make_actor(Role.ADMIN)
    response = await admin.client.post(
        "/api/v1/users",
        json={
            "email": "New.Person@Example.edu",
            "display_name": " New  Person ",
            "role": "faculty",
        },
        headers=admin.headers,
    )
    assert response.status_code == 201
    body = response.json()
    assert body["user"]["email"] == "new.person@example.edu"
    assert body["user"]["display_name"] == "New Person"
    assert body["user"]["must_change_password"] is True
    temporary = body["temporary_password"]
    assert temporary and len(temporary) >= 10
    # The new user can log in with it and is gated to the change-password page.
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"Origin": "http://test"}
    ) as c:
        await api_login(c, "new.person@example.edu", temporary)
        assert (await c.get("/api/v1/structure")).status_code == 403
    audit = await _audit(app, "user_created")
    assert any(r.details.get("email") == "new.person@example.edu" for r in audit)
    assert all(temporary not in str(r.details) for r in audit)


async def test_create_user_with_supplied_password_returns_none(
    make_actor: ActorFactory,
) -> None:
    admin = await make_actor(Role.ADMIN)
    response = await admin.client.post(
        "/api/v1/users",
        json={
            "email": "s@example.edu",
            "display_name": "S",
            "role": "student",
            "password": "chosen by admin 1",
        },
        headers=admin.headers,
    )
    assert response.status_code == 201
    assert response.json()["temporary_password"] is None


async def test_create_user_validation(make_actor: ActorFactory) -> None:
    admin = await make_actor(Role.ADMIN, email="admin@example.edu")
    base = {"display_name": "X", "role": "student"}
    bad_email = await admin.client.post(
        "/api/v1/users", json={**base, "email": "nope"}, headers=admin.headers
    )
    assert bad_email.status_code == 422
    short = await admin.client.post(
        "/api/v1/users",
        json={**base, "email": "x@example.edu", "password": "short"},
        headers=admin.headers,
    )
    assert short.status_code == 422
    duplicate = await admin.client.post(
        "/api/v1/users", json={**base, "email": "ADMIN@example.edu"}, headers=admin.headers
    )
    assert duplicate.status_code == 409
    bad_role = await admin.client.post(
        "/api/v1/users",
        json={**base, "email": "y@example.edu", "role": "root"},
        headers=admin.headers,
    )
    assert bad_role.status_code == 422


async def test_list_never_exposes_password_hashes(make_actor: ActorFactory) -> None:
    admin = await make_actor(Role.ADMIN)
    response = await admin.client.get("/api/v1/users")
    assert response.status_code == 200
    assert "password_hash" not in response.text
    assert "argon2" not in response.text


async def test_role_change_is_audited(app: FastAPI, make_actor: ActorFactory) -> None:
    admin = await make_actor(Role.ADMIN)
    other = await make_actor(Role.STUDENT)
    response = await admin.client.patch(
        f"/api/v1/users/{other.user.id}", json={"role": "faculty"}, headers=admin.headers
    )
    assert response.status_code == 200
    assert response.json()["role"] == "faculty"
    rows = await _audit(app, "user_role_changed")
    assert len(rows) == 1
    assert rows[0].actor_id == admin.user.id
    assert rows[0].details == {"from": "student", "to": "faculty"}


async def test_deactivate_revokes_sessions_and_blocks_login(
    app: FastAPI, make_actor: ActorFactory
) -> None:
    admin = await make_actor(Role.ADMIN)
    other = await make_actor(Role.STUDENT, email="gone@example.edu")
    response = await admin.client.patch(
        f"/api/v1/users/{other.user.id}", json={"is_active": False}, headers=admin.headers
    )
    assert response.status_code == 200
    assert (await other.client.get("/api/v1/auth/me")).status_code == 401
    async with app.state.database.session() as db:
        remaining = (
            (await db.execute(select(UserSession).where(UserSession.user_id == other.user.id)))
            .scalars()
            .all()
        )
    assert remaining == []
    token = (await other.client.get("/api/v1/auth/csrf")).json()["csrf_token"]
    again = await other.client.post(
        "/api/v1/auth/login",
        json={"email": "gone@example.edu", "password": TEST_PASSWORD},
        headers={"X-CSRF-Token": token},
    )
    assert again.status_code == 401
    assert len(await _audit(app, "user_deactivated")) == 1
    reactivate = await admin.client.patch(
        f"/api/v1/users/{other.user.id}", json={"is_active": True}, headers=admin.headers
    )
    assert reactivate.status_code == 200
    assert len(await _audit(app, "user_reactivated")) == 1


async def test_admin_cannot_demote_or_deactivate_self(make_actor: ActorFactory) -> None:
    admin = await make_actor(Role.ADMIN)
    await make_actor(Role.ADMIN)  # a second admin exists, so only the self-rule applies
    for payload in ({"role": "faculty"}, {"is_active": False}):
        response = await admin.client.patch(
            f"/api/v1/users/{admin.user.id}", json=payload, headers=admin.headers
        )
        assert response.status_code == 409, payload


async def test_sole_admin_cannot_remove_themselves(make_actor: ActorFactory) -> None:
    admin = await make_actor(Role.ADMIN)
    for payload in ({"role": "student"}, {"is_active": False}):
        response = await admin.client.patch(
            f"/api/v1/users/{admin.user.id}", json=payload, headers=admin.headers
        )
        assert response.status_code == 409, payload


async def test_last_active_admin_guard_holds_for_any_actor(
    app: FastAPI, make_actor: ActorFactory
) -> None:
    """Defence in depth: even a different (e.g. stale) admin actor cannot remove the last admin."""
    from app.core.errors import AppError
    from app.services import users as user_service

    sole = await make_actor(Role.ADMIN)
    ghost = User(  # an active admin that is not the target and not the one in the database
        id="f" * 32,
        email="ghost@example.edu",
        display_name="Ghost",
        role=Role.ADMIN,
        password_hash="x",
        is_active=True,
        must_change_password=False,
    )
    async with app.state.database.session() as db:
        for change in ({"role": Role.FACULTY}, {"is_active": False}):
            with pytest.raises(AppError) as caught:
                await user_service.update_user(db, ghost, sole.user.id, **change)
            assert caught.value.status_code == 409
        row = await db.get(User, sole.user.id)
        assert row is not None
        assert row.is_active
        assert row.role == Role.ADMIN


async def test_reset_password_returns_temp_once_and_revokes_sessions(
    app: FastAPI, make_actor: ActorFactory
) -> None:
    admin = await make_actor(Role.ADMIN)
    other = await make_actor(Role.STUDENT, email="r@example.edu")
    response = await admin.client.post(
        f"/api/v1/users/{other.user.id}/reset-password", headers=admin.headers
    )
    assert response.status_code == 200
    temporary = response.json()["temporary_password"]
    assert temporary
    assert response.json()["user"]["must_change_password"] is True
    assert (await other.client.get("/api/v1/auth/me")).status_code == 401
    # The temporary password is not retrievable afterwards and never stored in clear.
    listing = await admin.client.get("/api/v1/users")
    assert temporary not in listing.text
    fetched = await admin.client.get(f"/api/v1/users/{other.user.id}")
    assert temporary not in fetched.text
    async with app.state.database.session() as db:
        row = await db.get(User, other.user.id)
        assert row is not None
        assert temporary not in row.password_hash
        details = [str(r.details) for r in (await db.execute(select(AuditLog))).scalars()]
    assert all(temporary not in d for d in details)
    assert len(await _audit(app, "password_reset")) == 1
    # Old password no longer works, the temporary one does.
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"Origin": "http://test"}
    ) as c:
        token = (await c.get("/api/v1/auth/csrf")).json()["csrf_token"]
        old = await c.post(
            "/api/v1/auth/login",
            json={"email": "r@example.edu", "password": TEST_PASSWORD},
            headers={"X-CSRF-Token": token},
        )
        assert old.status_code == 401
        await api_login(c, "r@example.edu", temporary)


async def test_unknown_user_is_404(make_actor: ActorFactory) -> None:
    admin = await make_actor(Role.ADMIN)
    assert (await admin.client.get("/api/v1/users/" + "0" * 32)).status_code == 404
    assert (
        await admin.client.post(
            "/api/v1/users/" + "0" * 32 + "/reset-password", headers=admin.headers
        )
    ).status_code == 404
