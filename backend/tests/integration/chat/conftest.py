"""Fixtures for the chat tests: the app uses the fake embedder (vector index built for it) and a
fake LLM whose replies each test can script. No network anywhere."""

import json
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import Response
from langchain_core.messages import BaseMessage

from app.agent.state import AgentContext
from app.core.config import Settings
from app.ingestion.embedder import FakeEmbedder
from app.llm.factory import ChatModels
from app.llm.fake import FakeChatModel
from app.main import create_app
from app.models import Document, User
from app.models.enums import Role
from app.retrieval.access import AccessContext
from app.retrieval.hybrid import SearchScope
from tests.conftest import Actor, insert_user, make_settings
from tests.ingest_support import DIM, MODEL, configure_fake_index
from tests.integration.search.conftest import ingest_markdown, module_ids

ActorFactory = Callable[..., Awaitable[Actor]]

HEAPS = "# Heaps\n\nA binary heap keeps the largest item at the root of the tree.\n"
GRAPHS = "# Graphs\n\nDijkstra shortest path algorithm uses a priority queue over weighted edges.\n"
PLANTS = "# Plants\n\nChloroplasts convert sunlight, water and carbon dioxide into glucose.\n"


def judge_or_rewrite(messages: list[BaseMessage]) -> str:
    """Default fast model: every sufficiency judgement is SUFFICIENT; a rewrite returns the
    latest student message unchanged."""
    system = str(messages[0].content)
    if "SUFFICIENT" in system:
        return "SUFFICIENT"
    human = str(messages[-1].content)
    return human.rsplit("Latest student message:", 1)[-1].strip()


@dataclass
class Llms:
    main: FakeChatModel = field(default_factory=FakeChatModel)
    fast: FakeChatModel = field(default_factory=lambda: FakeChatModel(responder=judge_or_rewrite))

    def models(self) -> ChatModels:
        return ChatModels(
            main=self.main, fast=self.fast, main_name="fake-main", fast_name="fake-fast",
            provider="fake",
        )  # fmt: skip

    def judge_calls(self) -> int:
        return sum(1 for c in self.fast.calls if "SUFFICIENT" in str(c[0].content))

    def rewrite_calls(self) -> int:
        return len(self.fast.calls) - self.judge_calls()


@pytest.fixture
def llms() -> Llms:
    return Llms()


@pytest.fixture
def settings(tmp_path: Path, template_db: Path) -> Settings:
    shutil.copyfile(template_db, tmp_path / "test.db")
    configure_fake_index(tmp_path / "test.db")
    return make_settings(
        tmp_path, embedding_provider="fake", embedding_model=MODEL, embedding_dim=DIM
    )


@pytest.fixture
async def app(settings: Settings, llms: Llms) -> AsyncIterator[FastAPI]:
    application = create_app(
        settings,
        embedder_factory=lambda: FakeEmbedder(model=MODEL, dim=DIM),
        llm_factory=llms.models,
    )
    yield application
    await application.state.database.dispose()


@pytest.fixture
async def corpus(app: FastAPI, settings: Settings, seeded: None) -> dict[str, Any]:
    mods = await module_ids(app)
    docs: dict[str, Document] = {
        "heaps": await ingest_markdown(
            app, settings, module_id=mods["1.1.1"], title="Heaps", body=HEAPS
        ),
        "graphs": await ingest_markdown(
            app, settings, module_id=mods["1.1.2"], title="Graphs", body=GRAPHS
        ),
        "plants": await ingest_markdown(
            app, settings, module_id=mods["1.2.1"], title="Plants", body=PLANTS
        ),
    }
    return {"mods": mods, "docs": docs}


async def build_context(
    app: FastAPI,
    llms: Llms,
    *,
    scope: SearchScope,
    module_path: str | None = None,
    user: User | None = None,
) -> AgentContext:
    user = user or await insert_user(app, email="ctx@example.edu", role=Role.STUDENT)
    return AgentContext(
        access=AccessContext.from_user(user),
        scope=scope,
        module_path=module_path,
        scope_label="the module" if scope.level != "all" else "all material",
        database=app.state.database,
        settings=app.state.settings,
        embedder=app.state.query_embedder,
        models=llms.models(),
    )


def parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    for frame in body.split("\n\n"):
        if not frame.strip():
            continue
        name = ""
        data = ""
        for line in frame.split("\n"):
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data += line[len("data: ") :]
        events.append((name, json.loads(data)))
    return events


def sse_of(response: Response) -> list[tuple[str, dict[str, Any]]]:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return parse_sse(response.text)
