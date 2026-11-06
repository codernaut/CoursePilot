import re
from collections.abc import Awaitable, Callable

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.models import User
from app.models.enums import Role
from tests.conftest import TEST_PASSWORD, Actor, insert_user

ActorFactory = Callable[..., Awaitable[Actor]]


def _csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "no csrf_token field in page"
    return match.group(1)


async def _form_login(client: AsyncClient, email: str, password: str) -> tuple[int, str]:
    page = await client.get("/login")
    response = await client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": _csrf_from(page.text)},
        follow_redirects=False,
    )
    return response.status_code, response.text


async def test_login_page_renders(client: AsyncClient) -> None:
    response = await client.get("/login")
    assert response.status_code == 200
    assert "<h1>Log in</h1>" in response.text
    assert 'name="csrf_token"' in response.text
    assert '<label for="email">' in response.text
    assert "<script>" not in response.text  # no inline scripts


async def test_form_login_and_logout_flow(app: FastAPI, client: AsyncClient) -> None:
    await insert_user(app, email="p@example.edu", role=Role.STUDENT)
    status, _ = await _form_login(client, "p@example.edu", TEST_PASSWORD)
    assert status == 303
    home = await client.get("/")
    assert home.status_code == 200
    assert "P" in home.text
    logout = await client.post(
        "/logout", data={"csrf_token": _csrf_from(home.text)}, follow_redirects=False
    )
    assert logout.status_code == 303
    assert logout.headers["location"] == "/login"
    assert (await client.get("/", follow_redirects=False)).status_code == 303


async def test_form_login_failure_is_generic_and_identical(
    app: FastAPI, client: AsyncClient
) -> None:
    await insert_user(app, email="p@example.edu", role=Role.STUDENT)
    await insert_user(app, email="off@example.edu", role=Role.STUDENT, active=False)
    outcomes = [
        await _form_login(client, "nobody@example.edu", "whatever pass!"),
        await _form_login(client, "p@example.edu", "wrong password!"),
        await _form_login(client, "off@example.edu", TEST_PASSWORD),
    ]
    assert {status for status, _ in outcomes} == {401}
    errors = {re.search(r'role="alert">([^<]+)<', body).group(1) for _, body in outcomes}  # type: ignore[union-attr]
    assert errors == {"Invalid email or password."}


async def test_form_login_requires_valid_login_token(app: FastAPI, client: AsyncClient) -> None:
    await insert_user(app, email="p@example.edu", role=Role.STUDENT)
    missing = await client.post(
        "/login", data={"email": "p@example.edu", "password": TEST_PASSWORD}
    )
    assert missing.status_code == 403
    forged = await client.post(
        "/login",
        data={"email": "p@example.edu", "password": TEST_PASSWORD, "csrf_token": "forged"},
    )
    assert forged.status_code == 403
    token = _csrf_from((await client.get("/login")).text)
    evil = await client.post(
        "/login",
        data={"email": "p@example.edu", "password": TEST_PASSWORD, "csrf_token": token},
        headers={"Origin": "https://evil.example"},
    )
    assert evil.status_code == 403


async def test_api_login_requires_login_token_and_origin(app: FastAPI, client: AsyncClient) -> None:
    await insert_user(app, email="p@example.edu", role=Role.STUDENT)
    body = {"email": "p@example.edu", "password": TEST_PASSWORD}
    assert (await client.post("/api/v1/auth/login", json=body)).status_code == 403
    token = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
    wrong_origin = await client.post(
        "/api/v1/auth/login",
        json=body,
        headers={"X-CSRF-Token": token, "Origin": "https://evil.example"},
    )
    assert wrong_origin.status_code == 403


async def test_forced_password_change_flow_via_pages(app: FastAPI, client: AsyncClient) -> None:
    await insert_user(app, email="n@example.edu", role=Role.STUDENT, must_change_password=True)
    status, _ = await _form_login(client, "n@example.edu", TEST_PASSWORD)
    assert status == 303
    page = await client.get("/change-password")
    assert "must choose a new password" in page.text
    csrf = _csrf_from(page.text)
    mismatch = await client.post(
        "/change-password",
        data={
            "current_password": TEST_PASSWORD,
            "new_password": "a brand new password",
            "confirm_password": "different one here",
            "csrf_token": csrf,
        },
    )
    assert mismatch.status_code == 422
    done = await client.post(
        "/change-password",
        data={
            "current_password": TEST_PASSWORD,
            "new_password": "a brand new password",
            "confirm_password": "a brand new password",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert done.status_code == 303
    assert (await client.get("/")).status_code == 200
    async with app.state.database.session() as db:
        row = (await db.execute(select(User).where(User.email == "n@example.edu"))).scalar_one()
    assert row.must_change_password is False


async def test_admin_users_page_lists_and_creates(app: FastAPI, make_actor: ActorFactory) -> None:
    admin = await make_actor(Role.ADMIN, email="boss@example.edu")
    page = await admin.client.get("/admin/users")
    assert page.status_code == 200
    assert "boss@example.edu" in page.text
    assert "Create a user" in page.text
    created = await admin.client.post(
        "/admin/users",
        data={
            "email": "fresh@example.edu",
            "display_name": "Fresh Faculty",
            "role": "faculty",
            "password": "",
            "csrf_token": admin.csrf,
        },
    )
    assert created.status_code == 200
    assert "Temporary password for" in created.text
    temp = re.search(r'class="temp-password">([^<]+)<', created.text)
    assert temp
    again = await admin.client.get("/admin/users")
    assert temp.group(1) not in again.text  # shown once only
    assert "fresh@example.edu" in again.text


async def test_admin_page_reset_password_shows_temp_once(
    app: FastAPI, make_actor: ActorFactory
) -> None:
    admin = await make_actor(Role.ADMIN)
    other = await make_actor(Role.STUDENT)
    response = await admin.client.post(
        f"/admin/users/{other.user.id}/reset-password", data={"csrf_token": admin.csrf}
    )
    assert response.status_code == 200
    assert 'class="temp-password"' in response.text
    assert (await other.client.get("/api/v1/auth/me")).status_code == 401


async def test_admin_page_self_demotion_shows_error(make_actor: ActorFactory) -> None:
    admin = await make_actor(Role.ADMIN)
    response = await admin.client.post(
        f"/admin/users/{admin.user.id}/role", data={"role": "student", "csrf_token": admin.csrf}
    )
    assert response.status_code == 409
    assert "cannot demote or deactivate your own account" in response.text


async def test_structure_page_by_role(seeded: None, make_actor: ActorFactory) -> None:
    admin = await make_actor(Role.ADMIN)
    faculty = await make_actor(Role.FACULTY)
    student = await make_actor(Role.STUDENT)
    admin_html = (await admin.client.get("/structure")).text
    faculty_html = (await faculty.client.get("/structure")).text
    student_html = (await student.client.get("/structure")).text
    assert "Year 1 title" in admin_html and "Block 1 title" in admin_html
    assert "Module 1 title" in admin_html
    assert "Year 1 title" not in faculty_html and "Module 1 title" in faculty_html
    assert "title</label>" not in student_html and "Module 1" in student_html
    assert "Users</a>" in admin_html and "Users</a>" not in faculty_html


async def test_structure_page_rename_roundtrip(seeded: None, make_actor: ActorFactory) -> None:
    faculty = await make_actor(Role.FACULTY)
    html = (await faculty.client.get("/structure")).text
    action = re.search(r'action="(/structure/modules/[0-9a-f]+)"', html)
    assert action
    response = await faculty.client.post(
        action.group(1),
        data={"title": "<b>Bold</b>", "description": "d", "csrf_token": faculty.csrf},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Saved." in response.text
    assert "&lt;b&gt;Bold&lt;/b&gt;" in response.text  # autoescaped
    assert "<b>Bold</b>" not in response.text


async def test_home_lists_modules(seeded: None, make_actor: ActorFactory) -> None:
    student = await make_actor(Role.STUDENT)
    html = (await student.client.get("/")).text
    assert html.count("<strong>Module") == 27


async def test_static_files_are_public(client: AsyncClient) -> None:
    css = await client.get("/static/css/app.css")
    assert css.status_code == 200
    js = await client.get("/static/htmx.min.js")
    assert js.status_code == 200
    assert "htmx" in js.text[:200]


async def test_docs_and_openapi_are_login_gated(
    app: FastAPI, client: AsyncClient, make_actor: ActorFactory
) -> None:
    assert (await client.get("/api/v1/openapi.json")).status_code == 401
    assert (await client.get("/docs", follow_redirects=False)).status_code == 303
    actor = await make_actor(Role.STUDENT)
    spec = await actor.client.get("/api/v1/openapi.json")
    assert spec.status_code == 200
    assert "/api/v1/auth/login" in spec.json()["paths"]


async def test_docs_disabled_in_production(tmp_path, template_db) -> None:  # type: ignore[no-untyped-def]
    import shutil

    from app.main import create_app
    from tests.conftest import make_settings

    shutil.copyfile(template_db, tmp_path / "test.db")
    settings = make_settings(
        tmp_path, app_env="production", base_url="https://example.edu", secret_key="p" * 40
    )
    application = create_app(settings)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="https://example.edu"
        ) as c:
            assert (await c.get("/docs")).status_code == 404
            assert (await c.get("/api/v1/openapi.json")).status_code == 404
            headers = (await c.get("/login")).headers
            assert "strict-transport-security" in headers
    finally:
        await application.state.database.dispose()
