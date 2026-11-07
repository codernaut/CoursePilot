"""Runs agent turns: lazy chat models, the SQLite checkpointer and the event stream.

The checkpoint database (``CHECKPOINT_DB_PATH``) is separate from the application database. A
connection is opened for the duration of one turn (or one thread deletion) and closed afterwards,
so there is no long-lived handle to manage and importing this module opens nothing.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.agent.graph import build_graph
from app.agent.state import AgentContext, new_turn_input
from app.core.config import Settings
from app.llm.factory import ChatModels, LLMFactory, build_chat_models

BUSY_TIMEOUT_MS = 5000
RECURSION_LIMIT = 30


@dataclass
class TurnResult:
    answer: str = ""
    query: str = ""
    citations: list[dict[str, Any]] = field(default_factory=list)
    dropped_markers: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    trace: list[str] = field(default_factory=list)
    searched: list[str] = field(default_factory=list)
    used_web: bool = False  # a web source is among the kept citations
    web_queries: list[str] = field(default_factory=list)


class AgentRuntime:
    def __init__(self, settings: Settings, llm_factory: LLMFactory | None = None) -> None:
        self._settings = settings
        self._factory: LLMFactory = llm_factory or (lambda: build_chat_models(settings))
        self._models: ChatModels | None = None

    def models(self) -> ChatModels:
        """The chat models (created on first use). Raises ``ProviderNotConfigured``."""
        if self._models is None:
            self._models = self._factory()
        return self._models

    @asynccontextmanager
    async def _checkpointer(self) -> AsyncIterator[AsyncSqliteSaver]:
        path = Path(self._settings.checkpoint_db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path)
        try:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            saver = AsyncSqliteSaver(conn)
            await saver.setup()
            yield saver
        finally:
            await conn.close()

    async def stream_turn(
        self, *, thread_id: str, question: str, context: AgentContext
    ) -> AsyncIterator[tuple[str, Any]]:
        """Run one turn. Yields ``("status", text)``, ``("token", text)`` and finally
        ``("result", TurnResult)``. Cancelling the consumer cancels the graph."""
        merged: dict[str, Any] = {}
        async with self._checkpointer() as saver:
            graph: Any = build_graph(saver)
            config: Any = {
                "configurable": {"thread_id": thread_id},
                "recursion_limit": RECURSION_LIMIT,
            }
            async for mode, payload in graph.astream(
                new_turn_input(question),
                config,
                context=context,
                stream_mode=["custom", "updates"],
            ):
                if mode == "custom":
                    yield str(payload["type"]), payload["text"]
                else:
                    for update in payload.values():
                        if isinstance(update, dict):
                            merged.update({k: v for k, v in update.items() if k != "messages"})
        yield (
            "result",
            TurnResult(
                answer=str(merged.get("answer", "")),
                query=str(merged.get("query", "")),
                citations=list(merged.get("citations", [])),
                dropped_markers=list(merged.get("dropped_markers", [])),
                usage=dict(merged.get("usage", {})),
                trace=list(merged.get("trace", [])),
                searched=list(merged.get("searched", [])),
                used_web=bool(merged.get("used_web", False)),
                web_queries=list(merged.get("web_queries", [])),
            ),
        )

    async def delete_thread(self, thread_id: str) -> None:
        async with self._checkpointer() as saver:
            await saver.adelete_thread(thread_id)

    async def thread_exists(self, thread_id: str) -> bool:
        """Whether the checkpoint database still holds anything for the thread (for tests)."""
        async with self._checkpointer() as saver:
            state = await build_graph(saver).aget_state({"configurable": {"thread_id": thread_id}})
            return bool(state.values)
