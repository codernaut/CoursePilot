"""The Warm Studio redesign: every page renders for every role under the strict CSP, with the
theme and accent controls, and the usage chart is server-rendered SVG that escapes its data."""

import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from app.models import Module
from app.models.enums import Role
from app.schemas.admin import DayMessages, UsageOut
from app.web.common import make_templates
from tests.conftest import Actor

ActorFactory = Callable[..., Awaitable[Actor]]

COMMON = ["/", "/chat", "/search", "/materials", "/structure", "/change-password"]
ADMIN_ONLY = ["/admin/users", "/admin/jobs", "/admin/usage"]


@pytest.fixture
async def module(app: FastAPI, seeded: None) -> Module:
    async with app.state.database.read_session() as db:
        found = (await db.execute(select(Module).order_by(Module.number))).scalars().first()
    assert isinstance(found, Module)
    return found


def assert_strict_markup(html: str, where: str) -> None:
    assert not re.search(r"<script(?![^>]*\bsrc=)", html), f"inline script on {where}"
    assert not re.search(r"\sstyle\s*=", html, flags=re.I), f"style attribute on {where}"
    assert not re.search(r"\son[a-z]+\s*=", html, flags=re.I), f"event handler on {where}"
    assert "<style" not in html.lower(), f"style element on {where}"
    assert "javascript:" not in html.lower(), where
    assert "http://" not in html and "https://" not in html, f"external URL on {where}"


def assert_theme_controls(html: str) -> None:
    head = html.split("</head>")[0]
    scripts = re.findall(r"<script\b[^>]*>", head)
    assert scripts[0] == '<script src="/static/js/theme.js">'  # first, not deferred
    assert "data-theme-toggle" in html and 'aria-label="Dark theme"' in html
    assert 'aria-pressed="false"' in html
    for accent in ("crimson", "coral", "plum", "teal", "ocean"):
        assert re.search(rf'<button[^>]*data-accent="{accent}"[^>]*aria-label="[A-Za-z ]+"', html)
    assert 'role="group" aria-label="Accent colour"' in html


@pytest.mark.parametrize("role", [Role.STUDENT, Role.FACULTY, Role.ADMIN])
async def test_every_page_renders_without_inline_code_or_styles(
    make_actor: ActorFactory, module: Module, role: Role
) -> None:
    actor = await make_actor(role)
    paths = [
        *COMMON,
        f"/modules/{module.id}/materials",
        f"/modules/{module.id}/materials/documents",
    ]
    if role == Role.ADMIN:
        paths += [*ADMIN_ONLY, "/admin/jobs/table"]
    for path in paths:
        response = await actor.client.get(path)
        assert response.status_code == 200, path
        assert_strict_markup(response.text, path)
        csp = response.headers["content-security-policy"]
        assert "unsafe-inline" not in csp and "script-src 'self'" in csp
        if not path.endswith(("/documents", "/table")):  # fragments have no layout
            assert_theme_controls(response.text)
            assert 'id="main"' in response.text and "site-footer" in response.text


async def test_login_page_has_the_controls_and_no_inline_code(client: AsyncClient) -> None:
    response = await client.get("/login")
    assert response.status_code == 200
    assert_strict_markup(response.text, "/login")
    assert_theme_controls(response.text)
    assert "<h1>Log in</h1>" in response.text


async def test_admin_pages_are_not_visible_to_other_roles(
    make_actor: ActorFactory, module: Module
) -> None:
    student = await make_actor(Role.STUDENT)
    html = (await student.client.get("/")).text
    assert 'href="/admin/usage"' not in html and 'href="/admin/users"' not in html
    admin = await make_actor(Role.ADMIN)
    html = (await admin.client.get("/")).text
    assert 'href="/admin/usage"' in html and 'href="/admin/users"' in html


async def test_navigation_menu_and_user_menu_are_accessible(
    make_actor: ActorFactory, module: Module
) -> None:
    actor = await make_actor(Role.STUDENT)
    html = (await actor.client.get("/chat")).text
    assert 'aria-controls="site-menu"' in html and 'aria-expanded="false"' in html
    assert 'id="site-menu"' in html and 'aria-label="Main"' in html
    assert 'aria-current="page"' in html  # the Chat link
    assert 'action="/logout"' in html and 'href="/change-password"' in html


async def test_theme_js_and_css_are_served_with_the_right_content_types(
    client: AsyncClient,
) -> None:
    js = await client.get("/static/js/theme.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    for needle in ("cp-theme", "cp-accent", "data-theme", "data-accent", "prefers-color-scheme"):
        assert needle in js.text
    assert "innerHTML" not in js.text and "eval(" not in js.text
    css = await client.get("/static/css/app.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert "prefers-reduced-motion" in css.text and ":focus-visible" in css.text
    assert "@import" not in css.text and "url(http" not in css.text


# --- usage chart -----------------------------------------------------------------------------


def render_usage(days: list[DayMessages]) -> str:
    env = make_templates().env
    usage = UsageOut(
        days=14,
        messages_per_day=days,
        tokens_per_day=[],
        top_users=[],
        documents_by_status={},
        jobs_by_status={},
        storage_bytes=0,
        web_answers=0,
        total_answers=0,
    )
    request = SimpleNamespace(url=SimpleNamespace(path="/admin/usage"))
    return env.get_template("admin_usage.html").render(
        request=request,
        user=None,
        csrf_token=None,
        version_label="test",
        insecure_http=False,
        usage=usage,
        feedback=[],
        daily_limit=100,
    )


def test_usage_chart_is_inline_svg_with_one_bar_per_day() -> None:
    days = [DayMessages(day=f"2026-11-{d:02d}", questions=d * 2, answers=d) for d in range(1, 8)]
    html = render_usage(days)
    assert_strict_markup(html, "usage chart")
    svg = html[
        html.index('<svg class="chart"') : html.index("</svg>", html.index('<svg class="chart"'))
    ]
    assert 'role="img"' in svg and "<title" in svg and "<desc" in svg
    assert svg.count("<rect") == 7
    assert 'class="hi"' in svg  # the latest day is highlighted
    assert "2026-11-07: 14 questions, 7 answers" in svg
    heights = [float(h) for h in re.findall(r'<rect[^>]*height="([0-9.]+)"', svg)]
    assert heights == sorted(heights) and heights[-1] == pytest.approx(170.0)
    assert "Show the numbers as a table" in html  # the numbers are also available as text


def test_usage_chart_escapes_its_data() -> None:
    evil = '"><script>alert(1)</script>'
    html = render_usage([DayMessages(day=evil, questions=3, answers=1)])
    assert "<script>alert(1)" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_usage_chart_handles_no_data_and_all_zero_days() -> None:
    assert '<svg class="chart"' not in render_usage([])
    zero = render_usage([DayMessages(day="2026-11-01", questions=0, answers=0)])
    assert '<svg class="chart"' in zero and 'height="0.0"' in zero


# --- review additions ------------------------------------------------------------------------

STATIC = Path(__file__).resolve().parents[2] / "app" / "web" / "static"


async def test_html_has_no_hardcoded_theme_so_no_javascript_follows_the_system(
    client: AsyncClient,
) -> None:
    html = (await client.get("/login")).text
    root = re.search(r"<html[^>]*>", html)
    assert root and "data-theme" not in root.group(0)  # theme.js sets it before first paint


async def test_the_contrast_test_reads_the_stylesheet_that_is_served(client: AsyncClient) -> None:
    served = (await client.get("/static/css/app.css")).text
    on_disk = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    assert served.replace("\r\n", "\n") == on_disk.replace("\r\n", "\n")


def _ids_used_by(script: str) -> set[str]:
    text = (STATIC / "js" / script).read_text(encoding="utf-8")
    return set(re.findall(r'getElementById\("([\w-]+)"\)', text))


async def test_scripts_find_every_element_they_look_up(
    make_actor: ActorFactory, module: Module
) -> None:
    faculty = await make_actor(Role.FACULTY)
    chat = (await faculty.client.get("/chat")).text
    materials = (await faculty.client.get(f"/modules/{module.id}/materials")).text
    for script, page in (
        ("chat.js", chat),
        ("passage_panel.js", chat),
        ("materials.js", materials),
    ):
        ids = _ids_used_by(script)
        assert ids, script
        for element_id in ids:
            assert f'id="{element_id}"' in page, f"{script} needs #{element_id}"
    assert "data-delete-conversation" in (STATIC / "js" / "chat.js").read_text(encoding="utf-8")
    assert "data-passage-id" in (STATIC / "js" / "passage_panel.js").read_text(encoding="utf-8")


def test_theme_js_only_applies_whitelisted_values() -> None:
    js = (STATIC / "js" / "theme.js").read_text(encoding="utf-8")
    # a tampered localStorage value never reaches an attribute unless it is on the whitelist
    assert 'stored === "light" || stored === "dark"' in js
    assert "ACCENTS.indexOf(accent) >= 0" in js
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js and "document.write" not in js


def test_usage_chart_scrolls_instead_of_shrinking_on_phones() -> None:
    html = render_usage([DayMessages(day="2026-11-01", questions=1, answers=1)])
    assert 'class="chart-scroll"' in html and 'tabindex="0"' in html
    assert 'aria-label="Messages per day chart"' in html


def test_usage_chart_survives_huge_numbers() -> None:
    big = 10**15
    html = render_usage(
        [
            DayMessages(day="2026-11-01", questions=big, answers=big),
            DayMessages(day="2026-11-02", questions=1, answers=0),
        ]
    )
    for value in re.findall(r'<rect[^>]*?(?:x|y|width|height)="([^"]*)"', html):
        assert float(value) == float(value) and abs(float(value)) < 10**6, value
