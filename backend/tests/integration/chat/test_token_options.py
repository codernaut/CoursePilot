"""Token optimisations through the real graph: cache markers per provider, cache usage, the
output-length note, and a before/after size measurement of the answer prompt."""

from dataclasses import replace
from typing import Any

import pytest
from fastapi import FastAPI
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from app.agent import prompts
from app.agent.runtime import AgentRuntime
from app.core.config import Settings
from app.llm.factory import ChatModels
from app.retrieval.hybrid import RetrievedChunk, SearchScope
from tests.integration.chat.conftest import Llms, build_context
from tests.integration.chat.test_graph import QUESTION, run

# ``tests.integration.chat.conftest`` fixtures (app, corpus, llms, settings) are picked up by
# pytest from the package conftest; ``run`` is shared with the graph tests.


def marked(messages: list[BaseMessage]) -> list[int]:
    return [
        i
        for i, m in enumerate(messages)
        if isinstance(m.content, list)
        and any(isinstance(b, dict) and "cache_control" in b for b in m.content)
    ]


@pytest.fixture
def runtime(settings: Settings) -> AgentRuntime:
    return AgentRuntime(settings)


async def anthropic_context(app: FastAPI, llms: Llms, corpus: dict[str, Any], **kw: Any) -> Any:
    context = await build_context(
        app, llms, scope=SearchScope("module", corpus["mods"]["1.1.1"]), module_path="Y > B > M"
    )
    models = ChatModels(
        main=llms.main,
        fast=llms.fast,
        main_name="m",
        fast_name="f",
        provider=kw.get("provider", "anthropic"),
    )
    return replace(context, models=models)


async def test_anthropic_with_caching_marks_system_and_history(
    app: FastAPI, corpus: dict[str, Any], llms: Llms, runtime: AgentRuntime
) -> None:
    context = await anthropic_context(app, llms, corpus)
    await run(runtime, context, QUESTION, thread="c1")  # first turn: no history
    first = llms.main.calls[-1]
    assert marked(first) == [0]
    await run(runtime, context, "And what about its children?", thread="c1")
    second = llms.main.calls[-1]
    assert [type(m) for m in second] == [type(first[0]), HumanMessage, AIMessage, HumanMessage]
    assert marked(second) == [0, 2]
    assert '<source id="C1"' in str(second[-1].content) or "<sources>" in str(second[-1].content)


async def test_caching_can_be_switched_off(
    app: FastAPI, corpus: dict[str, Any], llms: Llms, runtime: AgentRuntime
) -> None:
    app.state.settings.llm_prompt_cache = False
    context = await anthropic_context(app, llms, corpus)
    await run(runtime, context, QUESTION, thread="c2")
    assert marked(llms.main.calls[-1]) == []


async def test_other_providers_never_get_cache_markers(
    app: FastAPI, corpus: dict[str, Any], llms: Llms, runtime: AgentRuntime
) -> None:
    base = await anthropic_context(app, llms, corpus)
    for provider in ("gemini", "fake"):
        llms.main.calls.clear()
        context = replace(base, models=replace(base.models, provider=provider))
        await run(runtime, context, QUESTION, thread=f"c-{provider}")
        assert marked(llms.main.calls[-1]) == []


async def test_cache_usage_is_recorded_in_the_turn_usage(
    app: FastAPI, corpus: dict[str, Any], llms: Llms, runtime: AgentRuntime
) -> None:
    llms.main.usage_extra = {"input_token_details": {"cache_read": 40, "cache_creation": 12}}
    context = await anthropic_context(app, llms, corpus)
    result, _ = await run(runtime, context, QUESTION, thread="c3")
    assert result.usage["cache_read_input_tokens"] == 40
    assert result.usage["cache_creation_input_tokens"] == 12


async def test_an_answer_cut_by_the_length_limit_gets_a_note_and_no_broken_marker(
    app: FastAPI, corpus: dict[str, Any], llms: Llms, runtime: AgentRuntime
) -> None:
    llms.main.responder = lambda m: "A heap keeps the maximum at the root [C1] and its children [C"
    llms.main.stop_reason = "max_tokens"
    context = await build_context(app, llms, scope=SearchScope("all"))
    result, _ = await run(runtime, context, QUESTION, thread="c4")
    assert result.answer.endswith(prompts.LENGTH_NOTE)
    body = result.answer.removesuffix(prompts.LENGTH_NOTE).rstrip()
    assert body == "A heap keeps the maximum at the root [C1] and its children"
    assert [c["marker"] for c in result.citations] == ["C1"]


async def test_a_normal_stop_adds_no_note(
    app: FastAPI, corpus: dict[str, Any], llms: Llms, runtime: AgentRuntime
) -> None:
    llms.main.stop_reason = "end_turn"
    context = await build_context(app, llms, scope=SearchScope("all"))
    result, _ = await run(runtime, context, QUESTION, thread="c5")
    assert prompts.LENGTH_NOTE not in result.answer


async def test_a_rewrite_cut_by_the_length_limit_falls_back_to_the_question(
    app: FastAPI, corpus: dict[str, Any], llms: Llms, runtime: AgentRuntime
) -> None:
    context = await build_context(app, llms, scope=SearchScope("all"))
    await run(runtime, context, QUESTION)
    llms.fast.responder = lambda m: (
        "SUFFICIENT" if "SUFFICIENT" in str(m[0].content) else "heap sort extracts the ro"
    )
    llms.fast.stop_reason = "max_tokens"
    second, _ = await run(runtime, context, "and how is it sorted?")
    assert second.query == "and how is it sorted?"


# -- measurement ---------------------------------------------------------------------------------


def _answer_prompt_chars(chunks: list[RetrievedChunk], max_chars: int, *, trim: bool = True) -> int:
    from app.agent.citations import Registry
    from app.agent.overlap import model_texts
    from app.retrieval.hybrid import truncate_text

    pairs = Registry.from_chunks(chunks).pairs()
    texts = (
        model_texts(chunks, max_chars)
        if trim
        else [truncate_text(c.text, max_chars) for c in chunks]
    )
    messages = prompts.build_answer_messages("Y › B › M", [], "q?", pairs, source_texts=texts)
    return sum(len(str(m.content)) for m in messages)


def test_leaner_context_is_measurably_smaller(capsys: pytest.CaptureFixture[str]) -> None:
    """Fixed scenario: eight consecutive 900-word-ish chunks of one document with a 100-word
    overlap (like the chunker produces), plus the same chunks cut to the old and new budgets."""
    from tests.unit.test_agent_citations import chunk

    def words(a: int, b: int) -> str:
        return " ".join(f"term{i:04d}" for i in range(a, b))

    def chunks(size: int, count: int) -> list[RetrievedChunk]:
        return [
            chunk(
                i + 1,
                document_id="d",
                ordinal=i,
                text=words(i * (size - 20), i * (size - 20) + size),
            )
            for i in range(count)
        ]

    big = chunks(450, 8)  # ~4,000 chars per chunk (far over the budget)
    small = chunks(100, 8)  # ~900 chars per chunk (merged slides, short sections)
    before = _answer_prompt_chars(big, 1500)
    after_big = _answer_prompt_chars(big[:6], 1000)
    before_small = _answer_prompt_chars(small, 1500)
    after_small = _answer_prompt_chars(small[:6], 1000)
    no_trim_small = _answer_prompt_chars(small[:6], 1000, trim=False)
    print(
        f"\nMEASURE long chunks : before(8 x 1500) {before} chars -> after(6 x 1000) {after_big}"
        f"\nMEASURE short chunks: before(8 x 1500) {before_small} chars -> after(6 x 1000) "
        f"{after_small} (without overlap trimming {no_trim_small})"
    )
    assert after_big < before * 0.75
    assert after_small < before_small * 0.85
    assert after_small < no_trim_small  # the overlap trimming itself removes text
