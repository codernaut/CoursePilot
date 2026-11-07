"""A chat turn logs the request (and nothing of the conversation): no question, answer, material
excerpt or source text ever reaches the log."""

import io
import json
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import pytest

from app.core.logging import configure_logging
from app.models.enums import Role
from tests.conftest import Actor
from tests.integration.chat.conftest import Llms, sse_of
from tests.integration.chat.test_chat_api import ask

ActorFactory = Callable[..., Awaitable[Actor]]
QUESTION = "What does a binary heap keep at the root, my secret question?"
ANSWER = "The root holds the largest item, a distinctive answer sentence [C1]."


@pytest.fixture
def log() -> Iterator[io.StringIO]:
    buffer = io.StringIO()
    configure_logging("DEBUG", json=True, stream=buffer)
    yield buffer
    configure_logging("INFO", json=True)


async def test_prompts_answers_and_material_are_not_logged(
    make_actor: ActorFactory, corpus: dict[str, Any], llms: Llms, log: io.StringIO
) -> None:
    llms.main.responder = lambda messages: ANSWER
    student = await make_actor(Role.STUDENT, email="asker@example.edu")
    sse_of(await ask(student, QUESTION, module_id=corpus["mods"]["1.1.1"]))

    text = log.getvalue()
    for fragment in (
        "secret question",
        "distinctive answer",
        "largest item at the root of the tree",  # the retrieved material
        "asker@example.edu",
    ):
        assert fragment not in text
    chat_lines = [
        json.loads(line)
        for line in text.splitlines()
        if line.startswith("{") and "/api/v1/chat" in line
    ]
    assert len(chat_lines) == 1 and chat_lines[0]["status"] == 200
    assert chat_lines[0]["user_id"] == student.user.id
