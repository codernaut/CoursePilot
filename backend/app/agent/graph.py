"""The agent graph (CLAUDE.md section 9): an explicit LangGraph ``StateGraph``.

    START -> rewrite -> retrieve -> assess --(insufficient and a wider tier exists)--> retrieve
                                        |--(last tier insufficient, web ok)--> web_search -> answer
                                        \\--(otherwise)--> answer -> validate_citations -> END

The order "material first, web last" lives in the graph, not in a prompt: the first retrieval
always happens (``rewrite`` always leads to ``retrieve``), widening goes module -> block -> all and
only when the current tier is insufficient, and the model cannot skip or redirect retrieval. The
``assess`` node's model judgement can only mark a tier insufficient. The ``web_search`` node sits
on the edge from ``assess`` to ``answer`` and runs only when the LAST tier is insufficient, the
module web toggle is on and a provider is configured (``assess`` decides this from the runtime
context, never from model output).

Nodes get who is asking and the scope from the runtime context (``AgentContext``), never from state
or model output. Status and token events are emitted through LangGraph's stream writer.
"""

from dataclasses import asdict
from typing import Any, Literal

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
)
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime

from app.agent import prompts, tools
from app.agent.citations import Registry, cited_payloads, strip_markers, validate_citations
from app.agent.state import (
    HISTORY_MAX_CHARS,
    HISTORY_MAX_TURNS,
    HISTORY_STORE_MAX_MESSAGES,
    AgentContext,
    AgentState,
)
from app.core.logging import get_logger
from app.retrieval.hybrid import RetrievedChunk
from app.websearch.base import WebResult

log = get_logger(__name__)

HISTORY_MESSAGE_CHARS = 2000
MAX_QUERY_CHARS = 500

Ctx = Runtime[AgentContext]

# The tiers searched from each starting scope (the same ladder as retrieval.hybrid.tier_scopes).
_LADDERS: dict[str, tuple[str, ...]] = {
    "module": ("module", "block", "all"),
    "block": ("block", "all"),
    "all": ("all",),
}


# -- helpers ---------------------------------------------------------------------------------------


def _emit(kind: str, **data: Any) -> None:
    get_stream_writer()({"type": kind, **data})


def _text(message: BaseMessage) -> str:
    return message.text if isinstance(message, AIMessage | AIMessageChunk) else str(message.content)


def trim_history(messages: list[AnyMessage]) -> list[BaseMessage]:
    """The part of the stored conversation sent to the model: the last ``HISTORY_MAX_TURNS``
    question/answer pairs, dropped pair by pair (oldest first) while over ``HISTORY_MAX_CHARS``,
    each message clipped. Always starts with a human message."""
    kept = [m for m in messages if isinstance(m, HumanMessage | AIMessage)]
    kept = kept[-HISTORY_MAX_TURNS * 2 :]
    while kept and isinstance(kept[0], AIMessage):
        kept = kept[1:]
    while len(kept) > 2 and sum(len(_text(m)) for m in kept) > HISTORY_MAX_CHARS:
        kept = kept[2:]
    clipped: list[BaseMessage] = []
    for message in kept:
        body = _text(message)[:HISTORY_MESSAGE_CHARS]
        clipped.append(
            AIMessage(content=body)
            if isinstance(message, AIMessage)
            else HumanMessage(content=body)
        )
    return clipped


def _history_text(history: list[BaseMessage]) -> str:
    return "\n".join(
        f"{'Student' if isinstance(m, HumanMessage) else 'Assistant'}: {_text(m)}" for m in history
    )


def _add_usage(current: dict[str, Any], message: BaseMessage | None) -> dict[str, Any]:
    usage = getattr(message, "usage_metadata", None) if message is not None else None
    merged = dict(current)
    merged["calls"] = int(merged.get("calls", 0)) + 1
    if usage:
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            merged[key] = int(merged.get(key, 0)) + int(usage.get(key, 0) or 0)
    return merged


def _chunks(state: AgentState) -> list[RetrievedChunk]:
    return [RetrievedChunk(**d) for d in state.get("chunks", [])]


def _web_results(state: AgentState) -> list[WebResult]:
    return [WebResult.from_dict(d) for d in state.get("web_results", [])]


def _trace(state: AgentState, entry: str) -> list[str]:
    return [*state.get("trace", []), entry]


def _scope_status(level: str, index: int, label: str) -> str:
    if index == 0:
        return f"Searching {label}…"
    if level == "block":
        return "Widening search to the whole block…"
    return "Widening search to all material…"


# -- nodes -----------------------------------------------------------------------------------------


async def rewrite(state: AgentState, runtime: Ctx) -> dict[str, Any]:
    """A standalone query from the recent history (fast model). The first message of a
    conversation is used as it is, without a model call."""
    question = state["question"]
    history = trim_history(state.get("messages", []))
    update: dict[str, Any] = {"trace": _trace(state, "rewrite"), "query": question}
    if not history:
        return update
    models = runtime.context.models
    try:
        reply = await models.fast.ainvoke(
            [
                SystemMessage(content=prompts.REWRITE_SYSTEM_PROMPT),
                HumanMessage(content=prompts.build_rewrite_human(_history_text(history), question)),
            ]
        )
    except Exception as exc:
        log.warning("rewrite_failed", error=type(exc).__name__)
        return update
    lines = [line.strip(" \"'`") for line in _text(reply).strip().splitlines() if line.strip()]
    if lines:
        update["query"] = lines[0][:MAX_QUERY_CHARS]
    update["usage"] = _add_usage(state.get("usage", {}), reply)
    return update


async def retrieve(state: AgentState, runtime: Ctx) -> dict[str, Any]:
    ctx = runtime.context
    index = state.get("tier_index", 0)
    ladder = _LADDERS[ctx.scope.level]
    _emit("status", text=_scope_status(ladder[min(index, len(ladder) - 1)], index, ctx.scope_label))
    result = await tools.search_material(ctx, query=state["query"], tier_index=index)
    return {
        "chunks": [asdict(c) for c in result.chunks],
        "sufficient": result.sufficient,
        "has_wider": result.has_wider,
        "searched": [*state.get("searched", []), result.scope.level],
        "trace": _trace(state, f"retrieve:{result.scope.level}"),
    }


async def assess(state: AgentState, runtime: Ctx) -> dict[str, Any]:
    """Is this tier enough? The similarity threshold and minimum hits were applied by
    ``retrieve``; a fast-model judgement may additionally mark a threshold-passing tier
    insufficient (it is only asked when a wider tier exists, and it can never skip retrieval or
    mark an insufficient tier sufficient). When the verdict is "insufficient" and a wider tier
    exists, the tier index moves on and the graph retrieves again."""
    chunks = _chunks(state)
    sufficient = bool(state.get("sufficient"))
    has_wider = bool(state.get("has_wider"))
    update: dict[str, Any] = {"trace": _trace(state, "assess")}
    if sufficient and has_wider and chunks:
        try:
            reply = await runtime.context.models.fast.ainvoke(
                [
                    SystemMessage(content=prompts.JUDGE_SYSTEM_PROMPT),
                    HumanMessage(content=prompts.build_judge_human(state["query"], chunks)),
                ]
            )
            update["trace"] = [*update["trace"], "judge"]
            update["usage"] = _add_usage(state.get("usage", {}), reply)
            if _text(reply).strip().upper().startswith("INSUFFICIENT"):
                sufficient = False
        except Exception as exc:  # the judgement is advisory: the threshold verdict stands
            log.warning("judge_failed", error=type(exc).__name__)
    update["sufficient"] = sufficient
    if not sufficient and has_wider:
        update["tier_index"] = state.get("tier_index", 0) + 1
    elif not sufficient:  # the last tier is insufficient: the web may be searched
        why_not = tools.web_available(runtime.context)
        update["web_allowed"] = not why_not
        update["web_status"] = why_not
    return update


def route_after_assess(state: AgentState) -> Literal["retrieve", "web_search", "answer"]:
    """Widen when the tier is insufficient and a wider one exists; when nothing wider exists and
    the web may be searched, go to ``web_search``; otherwise answer."""
    if not state.get("sufficient") and state.get("has_wider"):
        return "retrieve"
    if not state.get("sufficient") and state.get("web_allowed"):
        return "web_search"
    return "answer"


async def web_search(state: AgentState, runtime: Ctx) -> dict[str, Any]:
    """Search the web (at most WEB_MAX_QUERIES queries): first the rewritten standalone query,
    then, if that found nothing, the original wording of the question. Only query text leaves."""
    _emit("status", text="Searching the web…")
    candidates = [state["query"]]
    if state["question"] != state["query"]:
        candidates.append(state["question"])
    found = await tools.search_web(runtime.context, queries=candidates)
    status = "searched" if found.results else ("failed" if found.failed else "no_results")
    return {
        "web_results": [r.to_dict() for r in found.results],
        "web_queries": found.queries,
        "web_status": status,
        "trace": _trace(state, "web_search"),
    }


def _web_note(status: str) -> str | None:
    if status == "no_results":
        return "The web was searched for this question and returned no usable results."
    if status == "failed":
        return "The web search failed, so no web results are available."
    return None


def _not_found(status: str) -> str:
    if status == "not_configured":
        return prompts.NOT_FOUND_NO_WEB_MESSAGE
    if status in ("no_results", "failed"):
        return prompts.NOT_FOUND_WEB_MESSAGE
    return prompts.NOT_FOUND_MESSAGE


async def answer(state: AgentState, runtime: Ctx) -> dict[str, Any]:
    ctx = runtime.context
    chunks = _chunks(state)
    web = _web_results(state)
    status = state.get("web_status", "")
    trace = _trace(state, "answer")
    if not chunks and not web:
        message = _not_found(status)
        _emit("token", text=message)
        return {"answer": message, "trace": trace}
    _emit("status", text="Writing the answer…")
    registry = Registry.from_sources(chunks, web)
    messages: list[BaseMessage] = [
        SystemMessage(content=prompts.build_system_prompt(ctx.module_path)),
        *trim_history(state.get("messages", [])),
        HumanMessage(
            content=prompts.build_answer_human(
                state["question"], registry.pairs(), registry.web_pairs(), _web_note(status)
            )
        ),
    ]
    parts: list[str] = []
    final: AIMessageChunk | None = None
    async for piece in ctx.models.main.astream(messages):
        final = piece if final is None else final + piece
        text = piece.text
        if text:
            parts.append(text)
            _emit("token", text=text)
    final_text = "".join(parts)
    if not final_text.strip():  # e.g. a blocked or empty model reply: never store a blank answer
        final_text = prompts.EMPTY_ANSWER_MESSAGE
        _emit("token", text=final_text)
    return {
        "answer": final_text,
        "usage": _add_usage(state.get("usage", {}), final),
        "trace": trace,
    }


async def validate_citations_node(state: AgentState, runtime: Ctx) -> dict[str, Any]:
    registry = Registry.from_sources(_chunks(state), _web_results(state))
    checked = validate_citations(state.get("answer", ""), registry)
    stored = [
        HumanMessage(content=state["question"]),
        AIMessage(content=strip_markers(checked.text)),
    ]
    # Keep the checkpointed memory bounded: drop the oldest messages beyond the cap.
    existing = state.get("messages", [])
    overflow = max(0, len(existing) + len(stored) - HISTORY_STORE_MAX_MESSAGES)
    removals = [RemoveMessage(id=m.id) for m in existing[:overflow] if m.id]
    return {
        "answer": checked.text,
        "citations": cited_payloads(registry, checked.cited),
        "dropped_markers": checked.dropped,
        "used_web": any(m in registry.web for m in checked.cited),
        "chunks": [],  # the checkpoint keeps only the conversation, not copies of the material
        "web_results": [],  # nor of the web results
        "messages": [*removals, *stored],
        "trace": _trace(state, "validate_citations"),
    }


# -- graph -----------------------------------------------------------------------------------------


def build_graph(
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> CompiledStateGraph[AgentState, AgentContext, AgentState, AgentState]:
    graph = StateGraph(AgentState, context_schema=AgentContext)
    graph.add_node("rewrite", rewrite)
    graph.add_node("retrieve", retrieve)
    graph.add_node("assess", assess)
    graph.add_node("web_search", web_search)
    graph.add_node("answer", answer)
    graph.add_node("validate_citations", validate_citations_node)
    graph.add_edge(START, "rewrite")
    graph.add_edge("rewrite", "retrieve")
    graph.add_edge("retrieve", "assess")
    graph.add_conditional_edges("assess", route_after_assess, ["retrieve", "web_search", "answer"])
    graph.add_edge("web_search", "answer")
    graph.add_edge("answer", "validate_citations")
    graph.add_edge("validate_citations", END)
    return graph.compile(checkpointer=checkpointer)
