"""Agent state (checkpointed per conversation) and the run context (never checkpointed).

``AgentState`` holds only plain JSON-friendly values so the SQLite checkpointer can store it.
``AgentContext`` carries everything that must NOT come from the model or from the conversation:
who is asking (the access context), the starting scope, the database and the models. Nodes read it
through LangGraph's runtime context, so no model output or document text can change it.
"""

from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from app.core.config import Settings
from app.db.session import Database
from app.llm.factory import ChatModels
from app.retrieval.access import AccessContext
from app.retrieval.hybrid import SearchScope
from app.retrieval.query_embedding import QueryEmbedder
from app.websearch.base import WebSearchProvider

HISTORY_MAX_TURNS = 6  # question/answer pairs sent to the model
HISTORY_MAX_CHARS = 8000  # and at most this many characters of them
HISTORY_STORE_MAX_MESSAGES = 40  # what the checkpoint keeps


class AgentState(TypedDict, total=False):
    # Durable memory: the final text of earlier turns (human/AI pairs), trimmed.
    messages: Annotated[list[AnyMessage], add_messages]
    # Per-turn values, reset by the input of every turn.
    question: str
    query: str
    tier_index: int
    chunks: list[dict[str, Any]]  # the last tier searched, best first (RetrievedChunk as dicts)
    searched: list[str]  # levels searched in order, e.g. ["module", "block"]
    sufficient: bool
    has_wider: bool
    web_allowed: bool  # the last material tier is insufficient and web search may run
    web_status: str  # "" | "searched" | "no_results" | "failed" | "not_configured" | "disabled"
    web_results: list[dict[str, Any]]  # WebResult.to_dict(), cleared at the end of the turn
    web_queries: list[str]
    answer: str
    truncated: bool  # the answer stopped at the output-length limit
    used_web: bool
    citations: list[dict[str, Any]]
    dropped_markers: list[str]
    usage: dict[str, Any]
    trace: list[str]  # node names run in order (for tests and debugging)


@dataclass(frozen=True)
class AgentContext:
    access: AccessContext
    scope: SearchScope
    module_path: str | None  # Year > Block > Module for a module chat, else None
    scope_label: str  # what the status lines call the starting scope
    database: Database
    settings: Settings
    embedder: QueryEmbedder
    models: ChatModels
    # Web fallback (M6): the provider is None when web search is not configured; ``web_enabled``
    # is the module's toggle (always True for "All material": the global setting decides).
    web: WebSearchProvider | None = None
    web_enabled: bool = True


def new_turn_input(question: str) -> dict[str, Any]:
    """The input of one turn: the question plus a reset of every per-turn field."""
    return {
        "question": question,
        "query": "",
        "tier_index": 0,
        "chunks": [],
        "searched": [],
        "sufficient": False,
        "has_wider": False,
        "web_allowed": False,
        "web_status": "",
        "web_results": [],
        "web_queries": [],
        "answer": "",
        "truncated": False,
        "used_web": False,
        "citations": [],
        "dropped_markers": [],
        "usage": {},
        "trace": [],
    }
