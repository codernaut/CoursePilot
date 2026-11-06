from collections.abc import Awaitable, Callable

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from app.models import Block, Module, Year
from app.models.enums import Role
from tests.conftest import Actor, insert_user
from tests.integration.routes import Route, csrf_routes, route_table

ActorFactory = Callable[..., Awaitable[Actor]]
ROLES = [Role.STUDENT, Role.FACULTY, Role.ADMIN]


@pytest.fixture
async def ids(app: FastAPI, seeded: None) -> dict[str, str]:
    target = await insert_user(app, email="target@example.edu", role=Role.STUDENT)
    async with app.state.database.session() as db:
        year = (await db.execute(select(Year).order_by(Year.number))).scalars().first()
        block = (await db.execute(select(Block))).scalars().first()
        module = (await db.execute(select(Module))).scalars().first()
    assert year and block and module
    return {"user": target.id, "year": year.id, "block": block.id, "module": module.id}


# The table has static ids; build it once with placeholders and resolve at run time.
_PLACEHOLDER = {"user": "{user}", "year": "{year}", "block": "{block}", "module": "{module}"}
TABLE = route_table(_PLACEHOLDER)


def _resolve(route: Route, ids: dict[str, str]) -> Route:
    return Route(
        route.method,
        route.path.format(**ids),
        route.body,
        route.form,
        route.expected,
        route.tags,
    )


async def _call(client: AsyncClient, route: Route, headers: dict[str, str]) -> int:
    kwargs: dict[str, object] = {"headers": headers, "follow_redirects": False}
    if route.body is not None:
        kwargs["data" if route.form else "json"] = route.body
    response = await client.request(route.method, route.path, **kwargs)  # type: ignore[arg-type]
    return response.status_code


@pytest.mark.parametrize("route", TABLE, ids=lambda r: r.id)
async def test_anonymous_access(client: AsyncClient, ids: dict[str, str], route: Route) -> None:
    resolved = _resolve(route, ids)
    assert await _call(client, resolved, {}) == resolved.expected[0]


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("route", TABLE, ids=lambda r: r.id)
async def test_role_access(
    make_actor: ActorFactory, ids: dict[str, str], route: Route, role: Role
) -> None:
    actor = await make_actor(role)
    resolved = _resolve(route, ids)
    expected = resolved.expected[1 + ROLES.index(role)]
    assert await _call(actor.client, resolved, actor.headers) == expected


async def test_anonymous_api_errors_use_json_shape(client: AsyncClient) -> None:
    response = await client.get("/api/v1/users")
    assert response.status_code == 401
    assert response.json() == {
        "error": {"code": "unauthorized", "message": "Authentication required.", "details": {}}
    }


async def test_anonymous_page_redirects_to_login(client: AsyncClient) -> None:
    response = await client.get("/structure", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_anonymous_htmx_request_gets_hx_redirect(client: AsyncClient) -> None:
    response = await client.get("/structure", headers={"HX-Request": "true"})
    assert response.headers["HX-Redirect"] == "/login"


@pytest.mark.parametrize("route", csrf_routes(_PLACEHOLDER), ids=lambda r: r.id)
async def test_csrf_required_on_state_changing_routes(
    make_actor: ActorFactory, ids: dict[str, str], route: Route
) -> None:
    actor = await make_actor(Role.ADMIN)
    resolved = _resolve(route, ids)
    assert await _call(actor.client, resolved, {}) == 403  # missing
    assert await _call(actor.client, resolved, {"X-CSRF-Token": "not-the-token"}) == 403
    # The signed *login* token is not a session token.
    login_token = (await actor.client.get("/api/v1/auth/csrf")).json()["csrf_token"]
    assert await _call(actor.client, resolved, {"X-CSRF-Token": login_token}) == 403


@pytest.mark.parametrize("route", csrf_routes(_PLACEHOLDER), ids=lambda r: r.id)
async def test_origin_is_checked_on_state_changing_routes(
    make_actor: ActorFactory, ids: dict[str, str], route: Route
) -> None:
    actor = await make_actor(Role.ADMIN)
    resolved = _resolve(route, ids)
    evil = {**actor.headers, "Origin": "https://evil.example"}
    assert await _call(actor.client, resolved, evil) == 403
    # An empty Origin counts as absent, so the Referer decides.
    evil_referer = {**actor.headers, "Origin": "", "Referer": "https://evil.example/page"}
    assert await _call(actor.client, resolved, evil_referer) == 403
    null_origin = {**actor.headers, "Origin": "null"}
    assert await _call(actor.client, resolved, null_origin) == 403


async def test_csrf_form_field_is_accepted(make_actor: ActorFactory, ids: dict[str, str]) -> None:
    actor = await make_actor(Role.ADMIN)
    response = await actor.client.post(
        f"/structure/years/{ids['year']}",
        data={"title": "Via field", "csrf_token": actor.csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    wrong = await actor.client.post(
        f"/structure/years/{ids['year']}",
        data={"title": "Nope", "csrf_token": "bad"},
        follow_redirects=False,
    )
    assert wrong.status_code == 403


async def test_same_origin_referer_without_origin_is_allowed(
    make_actor: ActorFactory, ids: dict[str, str]
) -> None:
    actor = await make_actor(Role.ADMIN)
    headers = {**actor.headers, "Referer": "http://test/structure"}
    response = await actor.client.post(
        f"/structure/years/{ids['year']}", data={"title": "Ref"}, headers=headers
    )
    assert response.status_code == 303
    renamed = await actor.client.get("/api/v1/structure")
    assert any(y["title"] == "Ref" for y in renamed.json()["years"])


async def test_csrf_not_required_on_get(make_actor: ActorFactory) -> None:
    actor = await make_actor(Role.STUDENT)
    assert (await actor.client.get("/api/v1/structure")).status_code == 200
