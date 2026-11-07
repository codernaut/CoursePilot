"""Chat requests are limited per user per minute, on top of the daily quota."""

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI
from sqlalchemy import func, select

from app.models import Message
from app.models.enums import Role
from tests.conftest import Actor
from tests.integration.chat.conftest import Llms, sse_of
from tests.integration.chat.test_chat_api import ask

ActorFactory = Callable[..., Awaitable[Actor]]


async def message_count(app: FastAPI) -> int:
    async with app.state.database.read_session() as db:
        return int((await db.execute(select(func.count()).select_from(Message))).scalar_one())


async def test_chat_burst_is_limited_per_user_with_retry_after(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any], llms: Llms
) -> None:
    app.state.chat_limiter.limit = 2
    alice, bob = await make_actor(Role.STUDENT), await make_actor(Role.STUDENT)
    module_id = corpus["mods"]["1.1.1"]
    for _ in range(2):
        sse_of(await ask(alice, module_id=module_id))
    stored_before = await message_count(app)

    blocked = await ask(alice, module_id=module_id)
    assert blocked.status_code == 429
    error = blocked.json()["error"]
    assert error["code"] == "rate_limited" and error["details"]["limit_per_min"] == 2
    assert 1 <= int(blocked.headers["Retry-After"]) <= 60
    assert await message_count(app) == stored_before  # the refused request stored nothing

    sse_of(await ask(bob, module_id=module_id))  # another user has their own budget


async def test_the_daily_quota_still_applies_and_is_a_different_error(
    app: FastAPI, settings: Any, make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    settings.chat_daily_limit_per_user = 1
    student = await make_actor(Role.STUDENT)
    sse_of(await ask(student, module_id=corpus["mods"]["1.1.1"]))
    second = await ask(student, module_id=corpus["mods"]["1.1.1"])
    assert second.status_code == 429 and second.json()["error"]["code"] == "quota_exceeded"
    assert "Retry-After" not in second.headers
