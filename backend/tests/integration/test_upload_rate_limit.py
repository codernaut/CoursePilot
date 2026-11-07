"""Upload requests are limited per user per minute (429 rate_limited with Retry-After)."""

from collections.abc import Awaitable, Callable

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.models import Document, Module
from app.models.enums import Role
from tests.conftest import Actor
from tests.files import make_pdf
from tests.integration.test_documents_api import upload

ActorFactory = Callable[..., Awaitable[Actor]]


@pytest.fixture
async def module_id(app: FastAPI, seeded: None) -> str:
    async with app.state.database.read_session() as db:
        module = (await db.execute(select(Module).order_by(Module.number))).scalars().first()
    assert isinstance(module, Module)
    return str(module.id)


async def test_upload_requests_are_limited_per_user_with_retry_after(
    app: FastAPI, make_actor: ActorFactory, module_id: str
) -> None:
    app.state.upload_limiter.limit = 2
    first, second = await make_actor(Role.FACULTY), await make_actor(Role.FACULTY)
    for index in range(2):
        assert (
            await upload(first, module_id, (f"a{index}.pdf", make_pdf(f"body {index}")))
        ).status_code == 202

    blocked = await upload(first, module_id, ("c.pdf", make_pdf("body c")))
    assert blocked.status_code == 429
    error = blocked.json()["error"]
    assert error["code"] == "rate_limited" and error["details"]["limit_per_min"] == 2
    assert 1 <= int(blocked.headers["Retry-After"]) <= 60
    async with app.state.database.read_session() as db:
        titles = {d.title for d in (await db.execute(select(Document))).scalars()}
    assert "c" not in titles  # nothing of the refused request was stored

    # Another user has their own budget.
    assert (await upload(second, module_id, ("d.pdf", make_pdf("body d")))).status_code == 202


async def test_unauthorised_uploads_do_not_consume_the_limit(
    app: FastAPI, make_actor: ActorFactory, module_id: str
) -> None:
    app.state.upload_limiter.limit = 1
    student = await make_actor(Role.STUDENT)
    for _ in range(3):
        assert (await upload(student, module_id, ("a.pdf", make_pdf("x")))).status_code == 403
    faculty = await make_actor(Role.FACULTY)
    assert (await upload(faculty, module_id, ("a.pdf", make_pdf("x")))).status_code == 202
