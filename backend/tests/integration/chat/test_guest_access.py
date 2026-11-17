"""Temporary module access (M9): codes, redemption, guest sessions, the access filter in SQL,
forbidden routes, chat, cleanup and the pages. Uses the chat fixtures (fake embedder + fake LLM)."""

import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, select

from app.core.config import Settings
from app.db.base import utcnow
from app.models import (
    AccessCode,
    AuditLog,
    Conversation,
    GuestAccount,
    Message,
    User,
    UserSession,
)
from app.models.enums import Role
from app.retrieval.access import AccessContext
from app.retrieval.hybrid import SearchScope, get_passage
from app.services import access_codes
from app.services import search as search_service
from app.services.auth import LoginThrottle
from app.worker import purge_guests_once
from tests.conftest import Actor, insert_user
from tests.integration.chat.conftest import Llms, sse_of

ActorFactory = Callable[..., Awaitable[Actor]]
CODE_RE = re.compile(r"[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{3}")
HEAP_Q = "What does a binary heap keep at the root?"
GRAPH_Q = "Dijkstra shortest path algorithm priority queue"


@dataclass
class Guest:
    client: AsyncClient
    csrf: str
    code_id: str
    plain: str
    module_id: str
    user_id: str

    @property
    def headers(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.csrf}


async def new_client(app: FastAPI) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Origin": "http://test"},
    )


async def post_guest(client: AsyncClient, code: str, name: str = "Gus Guest") -> Response:
    token = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
    return await client.post(
        "/api/v1/auth/guest",
        json={"code": code, "name": name},
        headers={"X-CSRF-Token": token},
    )


@pytest.fixture
async def faculty(app: FastAPI) -> User:
    return await insert_user(app, email="teacher@example.edu", role=Role.FACULTY)


async def make_code(
    app: FastAPI, issuer: User, module_id: str, hours: int = 4, label: str | None = None
) -> tuple[AccessCode, str]:
    async with app.state.database.session() as db:
        return await access_codes.create_code(
            db, issuer, module_id, duration_hours=hours, label=label
        )


async def set_expiry(app: FastAPI, code_id: str, *, delta: timedelta) -> None:
    async with app.state.database.session() as db:
        code = await db.get(AccessCode, code_id)
        assert code is not None
        code.expires_at = utcnow() + delta
        await db.commit()


@pytest.fixture
async def join(app: FastAPI, faculty: User) -> AsyncIterator[Callable[..., Awaitable[Guest]]]:
    clients: list[AsyncClient] = []

    async def factory(
        module_id: str, *, hours: int = 4, name: str = "Gus Guest", plain: str | None = None
    ) -> Guest:
        code_id = ""
        if plain is None:
            code, plain = await make_code(app, faculty, module_id, hours)
            code_id = code.id
        client = await new_client(app)
        clients.append(client)
        response = await post_guest(client, plain, name)
        assert response.status_code == 200, response.text
        body = response.json()
        return Guest(client, body["csrf_token"], code_id, plain, module_id, body["user"]["id"])

    yield factory
    for client in clients:
        await client.aclose()


async def ask(who: Guest | Actor, message: str = HEAP_Q, **body: Any) -> Response:
    return await who.client.post(
        "/api/v1/chat", json={"message": message, **body}, headers=who.headers
    )


# -- creating, listing and revoking codes -----------------------------------------------------


async def test_create_list_revoke_flow(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    module_id = corpus["mods"]["1.1.1"]
    teacher = await make_actor(Role.FACULTY)
    created = await teacher.client.post(
        f"/api/v1/modules/{module_id}/access-codes",
        json={"duration_hours": 8, "label": "  Revision \n session "},
        headers=teacher.headers,
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert CODE_RE.fullmatch(body["code"])
    assert created.headers["cache-control"] == "no-store"
    assert body["label"] == "Revision session" and body["status"] == "active"
    assert body["code_hint"] == body["code"].replace("-", "")[-4:]
    assert body["redemptions"] == 0

    async with app.state.database.read_session() as db:
        row = await db.get(AccessCode, body["id"])
        assert row is not None
        assert row.code_hash == access_codes.hash_code(body["code"]) != body["code"]
        assert row.expires_at - row.created_at == timedelta(hours=8)
        assert body["code"] not in (row.label or "") + row.code_hash

    listing = await teacher.client.get(f"/api/v1/modules/{module_id}/access-codes")
    assert listing.status_code == 200
    items = listing.json()
    assert [i["id"] for i in items] == [body["id"]]
    assert "code" not in items[0] and body["code"] not in listing.text

    revoked = await teacher.client.post(
        f"/api/v1/access-codes/{body['id']}/revoke", headers=teacher.headers
    )
    assert revoked.status_code == 200 and revoked.json()["status"] == "revoked"
    again = await teacher.client.post(
        f"/api/v1/access-codes/{body['id']}/revoke", headers=teacher.headers
    )
    assert again.status_code == 409 and again.json()["error"]["code"] == "conflict"

    async with app.state.database.read_session() as db:
        actions = (
            (await db.execute(select(AuditLog.action).where(AuditLog.target_id == body["id"])))
            .scalars()
            .all()
        )
    assert sorted(actions) == ["access_code_created", "access_code_revoked"]


@pytest.mark.parametrize("hours", [2, 4, 8, 10, 12, 24])
async def test_all_offered_durations_work(
    app: FastAPI, faculty: User, corpus: dict[str, Any], hours: int
) -> None:
    code, _ = await make_code(app, faculty, corpus["mods"]["1.1.1"], hours)
    assert code.expires_at - code.created_at == timedelta(hours=hours)


@pytest.mark.parametrize("hours", [0, 1, 3, 48, -4])
async def test_other_durations_are_rejected(
    make_actor: ActorFactory, corpus: dict[str, Any], hours: int
) -> None:
    teacher = await make_actor(Role.FACULTY)
    response = await teacher.client.post(
        f"/api/v1/modules/{corpus['mods']['1.1.1']}/access-codes",
        json={"duration_hours": hours},
        headers=teacher.headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_unknown_module_is_404_and_admin_may_create(
    make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    admin = await make_actor(Role.ADMIN)
    missing = await admin.client.post(
        f"/api/v1/modules/{'0' * 32}/access-codes",
        json={"duration_hours": 4},
        headers=admin.headers,
    )
    assert missing.status_code == 404
    ok = await admin.client.post(
        f"/api/v1/modules/{corpus['mods']['1.1.1']}/access-codes",
        json={"duration_hours": 4},
        headers=admin.headers,
    )
    assert ok.status_code == 201


async def test_create_needs_csrf_and_a_same_origin_request(
    make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    teacher = await make_actor(Role.FACULTY)
    url = f"/api/v1/modules/{corpus['mods']['1.1.1']}/access-codes"
    no_csrf = await teacher.client.post(url, json={"duration_hours": 4})
    assert no_csrf.status_code == 403
    bad_origin = await teacher.client.post(
        url,
        json={"duration_hours": 4},
        headers={**teacher.headers, "Origin": "http://evil.example"},
    )
    assert bad_origin.status_code == 403


async def test_expired_codes_stay_listed_for_a_few_days_only(
    app: FastAPI, make_actor: ActorFactory, faculty: User, corpus: dict[str, Any]
) -> None:
    module_id = corpus["mods"]["1.1.1"]
    teacher = await make_actor(Role.FACULTY)
    recent, _ = await make_code(app, faculty, module_id)
    old, _ = await make_code(app, faculty, module_id)
    await set_expiry(app, recent.id, delta=timedelta(hours=-5))
    await set_expiry(app, old.id, delta=timedelta(days=-10))
    items = (await teacher.client.get(f"/api/v1/modules/{module_id}/access-codes")).json()
    assert [(i["id"], i["status"]) for i in items] == [(recent.id, "expired")]


# -- redemption -------------------------------------------------------------------------------


async def test_redeem_creates_a_guest_session_for_that_module(
    app: FastAPI, faculty: User, corpus: dict[str, Any]
) -> None:
    module_id = corpus["mods"]["1.1.1"]
    code, plain = await make_code(app, faculty, module_id, hours=2)
    async with await new_client(app) as client:
        # Case, spaces and dashes do not matter.
        loose = plain.lower().replace("-", " ")
        response = await post_guest(client, loose, "  Ada \n Lovelace ")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["user"]["role"] == "guest"
        assert body["guest"]["module_id"] == module_id
        assert body["guest"]["module_title"] == "Module 1"
        assert "session=" in response.headers["set-cookie"]
        assert "HttpOnly" in response.headers["set-cookie"]

        me = await client.get("/api/v1/auth/me")
        assert me.status_code == 200
        assert me.json()["user"]["display_name"] == "Ada Lovelace"
        assert me.json()["guest"]["module_id"] == module_id

    async with app.state.database.read_session() as db:
        stored = await db.get(AccessCode, code.id)
        assert stored is not None and stored.redemptions == 1
        user = await db.get(User, body["user"]["id"])
        assert user is not None
        assert user.role == Role.GUEST and user.email.endswith("@guest.invalid")
        assert user.password_hash == access_codes.UNUSABLE_PASSWORD_HASH
        session = (
            await db.execute(select(UserSession).where(UserSession.user_id == user.id))
        ).scalar_one()
        # The session never outlives the code (a 2 h code, a 12 h session lifetime).
        assert session.expires_at == stored.expires_at
        audited = (
            await db.execute(select(AuditLog).where(AuditLog.action == "guest_redeemed"))
        ).scalar_one()
        assert audited.target_id == code.id and plain not in str(audited.details)


async def test_session_lifetime_is_capped_by_session_ttl(
    app: FastAPI, faculty: User, corpus: dict[str, Any], settings: Settings
) -> None:
    code, plain = await make_code(app, faculty, corpus["mods"]["1.1.1"], hours=24)
    async with await new_client(app) as client:
        assert (await post_guest(client, plain)).status_code == 200
    async with app.state.database.read_session() as db:
        session = (await db.execute(select(UserSession))).scalars().one()
        assert session.expires_at < code.expires_at
        assert session.expires_at - utcnow() <= timedelta(hours=settings.session_ttl_hours)


async def test_every_failure_is_the_same_401(
    app: FastAPI, faculty: User, corpus: dict[str, Any]
) -> None:
    module_id = corpus["mods"]["1.1.1"]
    expired, expired_plain = await make_code(app, faculty, module_id)
    revoked, revoked_plain = await make_code(app, faculty, module_id)
    await set_expiry(app, expired.id, delta=timedelta(minutes=-1))
    async with app.state.database.session() as db:
        await access_codes.revoke_code(db, faculty, revoked.id)
    seen = set()
    for bad in (
        "AAAA-BBBB-CCC",
        "",
        "!!!",
        "x" * 64,
        expired_plain,
        revoked_plain,
    ):
        async with await new_client(app) as client:
            response = await post_guest(client, bad)
            assert "set-cookie" not in response.headers
            seen.add((response.status_code, response.text))
    assert seen == {
        (
            401,
            '{"error":{"code":"unauthorized","message":"That access code is not valid or '
            'has expired.","details":{}}}',
        )
    }


async def test_a_name_is_required(app: FastAPI, faculty: User, corpus: dict[str, Any]) -> None:
    _, plain = await make_code(app, faculty, corpus["mods"]["1.1.1"])
    async with await new_client(app) as client:
        for bad in ("", "   ", "x" * 41):
            assert (await post_guest(client, plain, bad)).status_code in (422,)


async def test_redeeming_needs_the_login_csrf_token_and_origin(
    app: FastAPI, faculty: User, corpus: dict[str, Any]
) -> None:
    _, plain = await make_code(app, faculty, corpus["mods"]["1.1.1"])
    async with await new_client(app) as client:
        body = {"code": plain, "name": "Gus"}
        assert (await client.post("/api/v1/auth/guest", json=body)).status_code == 403
        token = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
        evil = await client.post(
            "/api/v1/auth/guest",
            json=body,
            headers={"X-CSRF-Token": token, "Origin": "http://evil.example"},
        )
        assert evil.status_code == 403
        assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_failed_attempts_are_throttled_but_successes_are_not(
    app: FastAPI, faculty: User, corpus: dict[str, Any]
) -> None:
    app.state.guest_throttle = LoginThrottle(2)
    _, plain = await make_code(app, faculty, corpus["mods"]["1.1.1"])
    for _ in range(4):  # a class joining from one network is not locked out
        async with await new_client(app) as client:
            assert (await post_guest(client, plain)).status_code == 200
    async with await new_client(app) as client:
        for _ in range(2):
            assert (await post_guest(client, "AAAA-BBBB-CCC")).status_code == 401
        blocked = await post_guest(client, "AAAA-BBBB-CCC")
        assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) >= 1
        assert (await post_guest(client, plain)).status_code == 429  # even a valid code, for now


async def test_guest_cannot_use_password_login(
    app: FastAPI, join: Callable[..., Awaitable[Guest]], corpus: dict[str, Any]
) -> None:
    guest = await join(corpus["mods"]["1.1.1"])
    async with app.state.database.read_session() as db:
        user = await db.get(User, guest.user_id)
        assert user is not None
    async with await new_client(app) as client:
        token = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
        for password in ("!", "correct horse battery", ""):
            response = await client.post(
                "/api/v1/auth/login",
                json={"email": user.email, "password": password},
                headers={"X-CSRF-Token": token},
            )
            assert response.status_code == 401


# -- expiry and revocation ---------------------------------------------------------------------


async def test_access_ends_at_expiry_for_every_request(
    app: FastAPI, join: Callable[..., Awaitable[Guest]], corpus: dict[str, Any]
) -> None:
    mods = corpus["mods"]
    first = await join(mods["1.1.1"], name="One")
    second = await join(mods["1.1.1"], name="Two")  # its own code, so unaffected below
    # A second guest redeeming the first guest's code shares its period.
    same = await join(mods["1.1.1"], plain=first.plain, name="Three")
    assert (await first.client.get("/api/v1/auth/me")).status_code == 200
    assert (await same.client.get("/api/v1/auth/me")).status_code == 200

    await set_expiry(app, first.code_id, delta=timedelta(seconds=-1))
    for guest in (first, same):
        me = await guest.client.get("/api/v1/auth/me")
        assert me.status_code == 401 and me.json()["error"]["code"] == "access_ended"
        assert (await ask(guest)).status_code == 401
        page = await guest.client.get("/chat", follow_redirects=False)
        assert page.status_code == 303 and page.headers["location"] == "/login?access_ended=1"
    # A guest of another code is unaffected.
    assert (await second.client.get("/api/v1/auth/me")).status_code == 200


async def test_login_page_explains_ended_access(client: AsyncClient) -> None:
    page = await client.get("/login?access_ended=1")
    assert "Your temporary access has ended" in page.text


async def test_revoking_ends_every_guest_of_the_code_immediately(
    app: FastAPI,
    make_actor: ActorFactory,
    join: Callable[..., Awaitable[Guest]],
    corpus: dict[str, Any],
) -> None:
    teacher = await make_actor(Role.FACULTY)
    mods = corpus["mods"]
    first = await join(mods["1.1.1"])
    second = await join(mods["1.1.1"], plain=first.plain, name="Other")
    response = await teacher.client.post(
        f"/api/v1/access-codes/{first.code_id}/revoke", headers=teacher.headers
    )
    assert response.status_code == 200
    for guest in (first, second):
        me = await guest.client.get("/api/v1/auth/me")
        assert me.status_code == 401 and me.json()["error"]["code"] == "access_ended"
        assert (await guest.client.get("/api/v1/search?q=heap")).status_code == 401
    async with await new_client(app) as client:  # and the code cannot be used again
        assert (await post_guest(client, first.plain)).status_code == 401


# -- the access filter ------------------------------------------------------------------------


async def test_guest_retrieval_is_confined_to_its_module_in_sql(
    app: FastAPI,
    settings: Settings,
    join: Callable[..., Awaitable[Guest]],
    make_actor: ActorFactory,
    corpus: dict[str, Any],
) -> None:
    mods, docs = corpus["mods"], corpus["docs"]
    guest = await join(mods["1.1.1"])
    student = await make_actor(Role.STUDENT)
    async with app.state.database.read_session() as db:
        guest_user = await db.get(User, guest.user_id)
        assert guest_user is not None
        guest_access = AccessContext.from_user(guest_user)
        student_access = AccessContext.from_user(student.user)
        embedder = app.state.query_embedder

        async def titles(
            access: AccessContext, query: str, scope: SearchScope, widen: bool
        ) -> set[str]:
            outcome = await search_service.search(
                db,
                settings=settings,
                embedder=embedder,
                access=access,
                query=query,
                scope=scope,
                widen=widen,
            )
            return {c.document_title for tier in outcome.tiers for c in tier.chunks}

        # Account: the whole programme is visible.
        every = SearchScope("all")
        assert {"Heaps", "Graphs"} <= await titles(
            student_access, f"{HEAP_Q} {GRAPH_Q}", every, False
        )
        # Guest: scope "all", and tiered widening module -> block -> all, still only its module.
        assert await titles(guest_access, GRAPH_Q, every, False) <= {"Heaps"}
        assert await titles(guest_access, GRAPH_Q, SearchScope("module", mods["1.1.1"]), True) <= {
            "Heaps"
        }
        assert (
            await titles(guest_access, GRAPH_Q, SearchScope("module", mods["1.1.2"]), False)
            == set()
        )
        assert await titles(guest_access, "Chloroplasts sunlight glucose", every, True) <= {"Heaps"}
        assert await titles(guest_access, HEAP_Q, every, False) == {"Heaps"}

        # Passages: own module's chunk yes, another module's chunk never.
        own = (
            await search_service.search(
                db,
                settings=settings,
                embedder=embedder,
                access=guest_access,
                query=HEAP_Q,
                scope=SearchScope("module", mods["1.1.1"]),
            )
        ).chunks[0]
        other = (
            await search_service.search(
                db,
                settings=settings,
                embedder=embedder,
                access=student_access,
                query=GRAPH_Q,
                scope=SearchScope("module", mods["1.1.2"]),
            )
        ).chunks[0]
        assert await get_passage(db, access=guest_access, chunk_id=own.chunk_id) is not None
        assert await get_passage(db, access=guest_access, chunk_id=other.chunk_id) is None
        assert await get_passage(db, access=student_access, chunk_id=other.chunk_id) is not None
    assert docs["graphs"].module_id == mods["1.1.2"]

    # The same through the API (the passage never carries a document link for a guest).
    assert (await guest.client.get(f"/api/v1/chunks/{other.chunk_id}")).status_code == 404
    passage = await guest.client.get(f"/api/v1/chunks/{own.chunk_id}")
    assert passage.status_code == 200 and passage.json()["document_url"] == ""
    assert (await student.client.get(f"/api/v1/chunks/{own.chunk_id}")).json()["document_url"]


async def test_guest_filter_rechecks_the_code_inside_the_query(
    app: FastAPI,
    settings: Settings,
    join: Callable[..., Awaitable[Guest]],
    corpus: dict[str, Any],
) -> None:
    guest = await join(corpus["mods"]["1.1.1"])
    async with app.state.database.read_session() as db:
        user = await db.get(User, guest.user_id)
        assert user is not None
        access = AccessContext.from_user(user)

        async def hits() -> int:
            outcome = await search_service.search(
                db,
                settings=settings,
                embedder=app.state.query_embedder,
                access=access,
                query=HEAP_Q,
                scope=SearchScope("all"),
            )
            return len(outcome.chunks)

        assert await hits() > 0
    await set_expiry(app, guest.code_id, delta=timedelta(seconds=-1))
    async with app.state.database.read_session() as db:  # a stale AccessContext gets nothing
        outcome = await search_service.search(
            db,
            settings=settings,
            embedder=app.state.query_embedder,
            access=access,
            query=HEAP_Q,
            scope=SearchScope("all"),
        )
        assert outcome.chunks == []


async def test_guest_search_api_is_forced_into_its_module(
    join: Callable[..., Awaitable[Guest]], corpus: dict[str, Any]
) -> None:
    mods = corpus["mods"]
    guest = await join(mods["1.1.1"])
    for params in (
        {"q": GRAPH_Q, "module_id": mods["1.1.2"]},
        {"q": GRAPH_Q, "scope": "all", "widen": "true"},
        {"q": GRAPH_Q, "document_id": corpus["docs"]["graphs"].id},
    ):
        response = await guest.client.get("/api/v1/search", params=params)
        assert response.status_code == 200, response.text
        assert all(h["document_title"] == "Heaps" for h in response.json()["hits"])
        assert response.json()["tiers"][0]["level"] == "module"
    hits = (await guest.client.get("/api/v1/search", params={"q": HEAP_Q})).json()["hits"]
    assert [h["document_title"] for h in hits] == ["Heaps"]


# -- everything else is closed to guests ------------------------------------------------------


async def test_guest_is_refused_everywhere_else(
    app: FastAPI, join: Callable[..., Awaitable[Guest]], corpus: dict[str, Any]
) -> None:
    mods, docs = corpus["mods"], corpus["docs"]
    guest = await join(mods["1.1.1"])
    m, d = mods["1.1.1"], docs["heaps"].id
    api = "/api/v1"
    routes: list[tuple[str, str, dict[str, Any] | None]] = [
        ("GET", f"{api}/documents/{d}/download", None),
        ("GET", f"{api}/documents/{d}", None),
        ("PATCH", f"{api}/documents/{d}", {"title": "x"}),
        ("DELETE", f"{api}/documents/{d}", None),
        ("POST", f"{api}/documents/{d}/reindex", None),
        ("GET", f"{api}/modules/{m}/documents", None),
        ("GET", f"{api}/modules/{mods['1.1.2']}/documents", None),
        ("GET", f"{api}/modules/{m}", None),
        ("PATCH", f"{api}/modules/{m}", {"title": "x"}),
        ("GET", f"{api}/structure", None),
        ("GET", f"{api}/users", None),
        ("POST", f"{api}/users", {"email": "a@b.edu", "display_name": "A", "role": "student"}),
        ("GET", f"{api}/admin/usage", None),
        ("GET", f"{api}/admin/jobs", None),
        ("GET", f"{api}/admin/feedback", None),
        ("POST", f"{api}/modules/{m}/access-codes", {"duration_hours": 4}),
        ("GET", f"{api}/modules/{m}/access-codes", None),
        (
            "POST",
            f"{api}/auth/change-password",
            {"current_password": "a" * 12, "new_password": "b" * 12},
        ),
        ("POST", f"{api}/chat", {"message": "hi", "module_id": mods["1.1.2"]}),
    ]
    for method, path, body in routes:
        response = await guest.client.request(
            method, path, json=body, headers=guest.headers, follow_redirects=False
        )
        assert response.status_code == 403, (method, path, response.status_code, response.text)
        assert response.json()["error"]["code"] == "forbidden"

    upload = await guest.client.post(
        f"{api}/modules/{m}/documents",
        files={"file": ("a.md", b"# hi\n", "text/markdown")},
        headers=guest.headers,
    )
    assert upload.status_code == 403

    for path in (
        "/",
        "/materials",
        f"/modules/{m}/materials",
        "/structure",
        "/admin/users",
        "/admin/usage",
        "/change-password",
    ):
        page = await guest.client.get(path, follow_redirects=False)
        assert page.status_code == 303 and page.headers["location"] == "/chat", path
    form = await guest.client.post(
        f"/modules/{m}/access-codes",
        data={"csrf_token": guest.csrf, "duration_hours": "4"},
        follow_redirects=False,
    )
    assert form.status_code == 403


async def test_guest_may_log_out_and_the_session_is_gone(
    app: FastAPI, join: Callable[..., Awaitable[Guest]], corpus: dict[str, Any]
) -> None:
    guest = await join(corpus["mods"]["1.1.1"])
    assert (
        await guest.client.post("/api/v1/auth/logout", headers=guest.headers)
    ).status_code == 200
    assert (await guest.client.get("/api/v1/auth/me")).status_code == 401


# -- guest chat -------------------------------------------------------------------------------


async def test_guest_chat_end_to_end_is_confined_to_the_module(
    app: FastAPI,
    join: Callable[..., Awaitable[Guest]],
    make_actor: ActorFactory,
    corpus: dict[str, Any],
    llms: Llms,
) -> None:
    llms.main.responder = lambda m: "A heap keeps the largest item at the root [C1]."
    mods = corpus["mods"]
    guest = await join(mods["1.1.1"])
    response = await ask(guest)  # no module_id: the guest's module is used, never "All material"
    events = sse_of(response)
    names = [n for n, _ in events]
    assert names[-2:] == ["citations", "done"]
    cited = next(d for n, d in events if n == "citations")["citations"]
    assert cited and {c["document_title"] for c in cited} == {"Heaps"}

    conversation_id = response.headers["x-conversation-id"]
    async with app.state.database.read_session() as db:
        conversation = await db.get(Conversation, conversation_id)
        assert conversation is not None
        assert conversation.module_id == mods["1.1.1"] and conversation.user_id == guest.user_id
    detail = await guest.client.get(f"/api/v1/conversations/{conversation_id}")
    assert detail.status_code == 200

    # Another module is refused, and a question about it finds nothing outside the module.
    assert (await ask(guest, module_id=mods["1.1.2"])).status_code == 403
    llms.main.responder = lambda m: "Nothing found."
    graph_events = sse_of(await ask(guest, GRAPH_Q))
    graph_cited = next(d for n, d in graph_events if n == "citations")["citations"]
    assert all(c["document_title"] != "Graphs" for c in graph_cited)

    # Private to its owner: another guest of the same code and an account get 404.
    other = await join(mods["1.1.1"], plain=guest.plain, name="Someone else")
    student = await make_actor(Role.STUDENT)
    for viewer in (other, student):
        assert (
            await viewer.client.get(f"/api/v1/conversations/{conversation_id}")
        ).status_code == 404
    assert (await other.client.get("/api/v1/conversations")).json() == []


async def test_guest_conversation_page_and_delete(
    join: Callable[..., Awaitable[Guest]], corpus: dict[str, Any], llms: Llms
) -> None:
    llms.main.responder = lambda m: "Heaps keep the largest item at the root [C1]."
    guest = await join(corpus["mods"]["1.1.1"])
    conversation_id = (await ask(guest)).headers["x-conversation-id"]
    assert (await guest.client.get(f"/chat/{conversation_id}")).status_code == 200
    deleted = await guest.client.delete(
        f"/api/v1/conversations/{conversation_id}", headers=guest.headers
    )
    assert deleted.status_code == 204


# -- cleanup ----------------------------------------------------------------------------------


async def test_cleanup_removes_sessions_at_once_and_chats_after_the_grace(
    app: FastAPI,
    settings: Settings,
    join: Callable[..., Awaitable[Guest]],
    corpus: dict[str, Any],
    llms: Llms,
) -> None:
    llms.main.responder = lambda m: "A heap keeps the largest item at the root [C1]."
    guest = await join(corpus["mods"]["1.1.1"], name="Temp Guest")
    other_code_guest = await join(corpus["mods"]["1.1.1"], name="Still Here")
    response = await ask(guest)
    conversation_id = response.headers["x-conversation-id"]
    assert await app.state.agent_runtime.thread_exists(conversation_id)

    async with app.state.database.session() as db:  # nothing has ended yet
        assert await access_codes.purge_ended_guests(db) == access_codes.GuestPurge()

    await set_expiry(app, guest.code_id, delta=timedelta(minutes=-10))
    async with app.state.database.session() as db:
        purged = await access_codes.purge_ended_guests(db)
    assert purged.sessions == 1 and purged.guests == 0 and purged.thread_ids == ()
    async with app.state.database.read_session() as db:
        assert (await db.get(Conversation, conversation_id)) is not None  # within the grace
        assert (await db.get(User, guest.user_id)) is not None

    await set_expiry(app, guest.code_id, delta=timedelta(hours=-2))
    result = await purge_guests_once(app.state.database, settings)
    assert result.guests == 1 and result.thread_ids == (conversation_id,)
    assert not await app.state.agent_runtime.thread_exists(conversation_id)
    async with app.state.database.read_session() as db:
        assert (await db.get(Conversation, conversation_id)) is None
        assert (await db.get(User, guest.user_id)) is None
        assert (await db.execute(select(func.count()).select_from(Message))).scalar_one() == 0
        assert (
            await db.execute(
                select(func.count())
                .select_from(GuestAccount)
                .where(GuestAccount.user_id == guest.user_id)
            )
        ).scalar_one() == 0
        code = await db.get(AccessCode, guest.code_id)
        assert code is not None and code.redemptions == 1  # the code row stays
        audit_row = (
            (await db.execute(select(AuditLog).where(AuditLog.action == "guest_redeemed")))
            .scalars()
            .all()
        )
        assert len(audit_row) == 2 and {a.actor_id for a in audit_row} == {
            None,
            other_code_guest.user_id,
        }
    # The guest of the other (still valid) code is untouched.
    assert (await other_code_guest.client.get("/api/v1/auth/me")).status_code == 200


async def test_revoked_code_is_cleaned_up_too(
    app: FastAPI,
    make_actor: ActorFactory,
    join: Callable[..., Awaitable[Guest]],
    corpus: dict[str, Any],
) -> None:
    teacher = await make_actor(Role.FACULTY)
    guest = await join(corpus["mods"]["1.1.1"])
    await teacher.client.post(
        f"/api/v1/access-codes/{guest.code_id}/revoke", headers=teacher.headers
    )
    async with app.state.database.session() as db:
        purged = await access_codes.purge_ended_guests(db)
        assert purged.sessions == 1
        later = await access_codes.purge_ended_guests(db, now=utcnow() + timedelta(hours=2))
    assert later.guests == 1


# -- admin views ------------------------------------------------------------------------------


async def test_guests_are_not_accounts_for_admins(
    make_actor: ActorFactory,
    join: Callable[..., Awaitable[Guest]],
    corpus: dict[str, Any],
    llms: Llms,
) -> None:
    llms.main.responder = lambda m: "A heap keeps the largest item at the root [C1]."
    admin = await make_actor(Role.ADMIN)
    guest = await join(corpus["mods"]["1.1.1"], name="Visitor")
    await ask(guest)

    users = (await admin.client.get("/api/v1/users")).json()
    assert all(u["role"] != "guest" and "guest.invalid" not in u["email"] for u in users)
    assert (await admin.client.get(f"/api/v1/users/{guest.user_id}")).status_code == 404
    assert (
        await admin.client.patch(
            f"/api/v1/users/{guest.user_id}", json={"is_active": False}, headers=admin.headers
        )
    ).status_code == 404
    assert (
        await admin.client.post(
            "/api/v1/users",
            json={"email": "g@example.edu", "display_name": "G", "role": "guest"},
            headers=admin.headers,
        )
    ).status_code == 422
    page = await admin.client.get("/admin/users")
    assert "guest.invalid" not in page.text and "Visitor" not in page.text

    usage = (await admin.client.get("/api/v1/admin/usage")).json()
    assert usage["guest_messages_in_period"] == 1 and usage["guest_messages_today"] == 1
    assert all("guest.invalid" not in u["email"] for u in usage["top_users"])
    usage_page = await admin.client.get("/admin/usage")
    assert "Guests (temporary access)" in usage_page.text
    assert "guest.invalid" not in usage_page.text


# -- pages ------------------------------------------------------------------------------------

INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)", re.I)


def assert_csp_clean(html: str) -> None:
    assert not INLINE_SCRIPT.search(html)
    assert " style=" not in html and "<style" not in html.lower()
    assert not re.search(r"\son[a-z]+=", html)


async def test_login_page_has_the_guest_card(client: AsyncClient) -> None:
    page = await client.get("/login")
    assert "Have a module access code?" in page.text
    assert 'action="/login/guest"' in page.text and 'name="code"' in page.text
    assert_csp_clean(page.text)


def login_token(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


async def test_guest_login_form_success_and_failure(
    app: FastAPI, faculty: User, corpus: dict[str, Any]
) -> None:
    _, plain = await make_code(app, faculty, corpus["mods"]["1.1.1"])
    async with await new_client(app) as client:
        token = login_token((await client.get("/login")).text)
        bad = await client.post(
            "/login/guest",
            data={"csrf_token": token, "code": "AAAA-BBBB-CCC", "name": "<b>Gus</b>"},
        )
        assert bad.status_code == 401
        assert "That access code is not valid or has expired." in bad.text
        assert "&lt;b&gt;Gus&lt;/b&gt;" in bad.text and "<b>Gus</b>" not in bad.text
        no_token = await client.post("/login/guest", data={"code": plain, "name": "Gus"})
        assert no_token.status_code == 403
        ok = await client.post(
            "/login/guest",
            data={"csrf_token": token, "code": plain, "name": "<b>Gus</b>"},
            follow_redirects=False,
        )
        assert ok.status_code == 303 and ok.headers["location"] == "/chat"

        chat = await client.get("/chat")
        assert chat.status_code == 200
        html = chat.text
        assert_csp_clean(html)
        assert "Guest &middot; Module 1" in html.replace("·", "&middot;")
        assert "data-local-time" in html
        assert "&lt;b&gt;Gus&lt;/b&gt;" in html and "<b>Gus</b>" not in html
        assert 'href="/materials"' not in html and 'href="/structure"' not in html
        assert 'href="/search"' in html and 'href="/chat"' in html
        assert "All material</option>" not in html
        assert "Change password" not in html
        search = await client.get("/search?q=heap")
        assert search.status_code == 200
        assert "Widen automatically" not in search.text and "search-module" not in search.text
        assert_csp_clean(search.text)


async def test_materials_page_panel_for_faculty_and_admin_only(
    make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    m = corpus["mods"]["1.1.1"]
    student = await make_actor(Role.STUDENT)
    assert "Temporary access" not in (await student.client.get(f"/modules/{m}/materials")).text
    home_student = (await student.client.get("/")).text
    assert "#temporary-access" not in home_student
    for role in (Role.FACULTY, Role.ADMIN):
        actor = await make_actor(role)
        html = (await actor.client.get(f"/modules/{m}/materials")).text
        assert 'id="temporary-access"' in html and "Create access code" in html
        for hours in (2, 4, 8, 10, 12, 24):
            assert f'<option value="{hours}"' in html
        assert (
            f'href="/modules/{m}/materials#temporary-access"' in (await actor.client.get("/")).text
        )
        assert_csp_clean(html)


async def test_creating_a_code_on_the_page_shows_it_once_and_escapes_the_label(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    m = corpus["mods"]["1.1.1"]
    teacher = await make_actor(Role.FACULTY)
    created = await teacher.client.post(
        f"/modules/{m}/access-codes",
        data={
            "csrf_token": teacher.csrf,
            "duration_hours": "8",
            "label": "<script>alert(1)</script>",
        },
    )
    assert created.status_code == 200
    assert created.headers["cache-control"] == "no-store"
    html = created.text
    plain = CODE_RE.search(html)
    assert plain is not None
    assert "Your new access code" in html and "only time the code is shown" in html
    assert 'id="copy-code"' in html and "access_codes.js" in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html and "<script>alert(1)" not in html
    assert_csp_clean(html)

    again = (await teacher.client.get(f"/modules/{m}/materials")).text
    assert plain.group(0) not in again and "Your new access code" not in again
    assert f"&bull;&bull;&bull;&bull;{plain.group(0).replace('-', '')[-4:]}" in again
    assert "Revoke" in again

    # The code works for a guest, and revoking on the page ends it.
    async with await new_client(app) as client:
        assert (await post_guest(client, plain.group(0))).status_code == 200
    async with app.state.database.read_session() as db:
        code = (await db.execute(select(AccessCode))).scalars().one()
    revoked = await teacher.client.post(
        f"/modules/{m}/access-codes/{code.id}/revoke",
        data={"csrf_token": teacher.csrf},
        follow_redirects=False,
    )
    assert revoked.status_code == 303
    after = (await teacher.client.get(revoked.headers["location"])).text
    assert "was revoked" in after and "Revoked" in after
    # A code of another module cannot be revoked through this module's URL.
    other = await teacher.client.post(
        f"/modules/{corpus['mods']['1.1.2']}/access-codes/{code.id}/revoke",
        data={"csrf_token": teacher.csrf},
    )
    assert other.status_code == 404


async def test_page_rejects_an_invalid_duration(
    make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    teacher = await make_actor(Role.FACULTY)
    response = await teacher.client.post(
        f"/modules/{corpus['mods']['1.1.1']}/access-codes",
        data={"csrf_token": teacher.csrf, "duration_hours": "7"},
    )
    assert response.status_code == 422 and "Choose a duration" in response.text


async def test_revoking_during_a_streaming_turn_stores_and_cites_nothing(
    app: FastAPI,
    join: Callable[..., Awaitable[Guest]],
    corpus: dict[str, Any],
    llms: Llms,
) -> None:
    """Access that ends while the answer is being written must not produce a stored answer or
    citations (the code is re-checked before the turn is persisted)."""
    import sqlite3

    guest = await join(corpus["mods"]["1.1.1"])
    db_path = app.state.settings.database_url.split("///", 1)[1]

    def revoke_then_answer(_: Any) -> str:
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE access_codes SET revoked_at = datetime('now')")
        conn.commit()
        conn.close()
        return "A heap keeps the largest item at the root [C1]."

    llms.main.responder = revoke_then_answer
    events = sse_of(await ask(guest))
    names = [n for n, _ in events]
    assert "citations" not in names and "done" not in names
    assert names[-1] == "error"
    async with app.state.database.read_session() as db:
        stored = (
            await db.execute(
                select(func.count()).select_from(Message).where(Message.role == "assistant")
            )
        ).scalar_one()
    assert stored == 0


async def test_natural_expiry_gives_the_access_ended_message_not_a_generic_401(
    app: FastAPI, join: Callable[..., Awaitable[Guest]], corpus: dict[str, Any]
) -> None:
    """The guest session ends exactly when the code does; the guest must still be told why."""
    guest = await join(corpus["mods"]["1.1.1"])
    past = utcnow() - timedelta(seconds=1)
    async with app.state.database.session() as db:
        code = await db.get(AccessCode, guest.code_id)
        assert code is not None
        code.expires_at = past
        for row in (await db.execute(select(UserSession))).scalars():
            row.expires_at = past
        await db.commit()
    me = await guest.client.get("/api/v1/auth/me")
    assert me.status_code == 401 and me.json()["error"]["code"] == "access_ended"
    page = await guest.client.get("/chat", follow_redirects=False)
    assert page.headers["location"] == "/login?access_ended=1"
