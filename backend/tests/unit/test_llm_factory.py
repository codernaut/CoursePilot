import asyncio
from pathlib import Path

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage, SystemMessage

from app.core.config import Settings
from app.llm import ProviderNotConfigured, build_chat_models
from app.llm.fake import FakeChatModel
from tests.conftest import make_settings


def settings_for(tmp_path: Path, **overrides: object) -> Settings:
    return make_settings(tmp_path, **overrides)


def test_anthropic_without_a_key_is_not_configured(tmp_path: Path) -> None:
    with pytest.raises(ProviderNotConfigured) as caught:
        build_chat_models(settings_for(tmp_path, llm_provider="anthropic"))
    assert "ANTHROPIC_API_KEY" in caught.value.hint


def test_gemini_without_a_key_is_not_configured(tmp_path: Path) -> None:
    with pytest.raises(ProviderNotConfigured) as caught:
        build_chat_models(settings_for(tmp_path, llm_provider="gemini", anthropic_api_key="x"))
    assert "GOOGLE_API_KEY" in caught.value.hint  # the other provider's key does not count


def test_a_blank_key_counts_as_missing(tmp_path: Path) -> None:
    with pytest.raises(ProviderNotConfigured):
        build_chat_models(settings_for(tmp_path, llm_provider="anthropic", anthropic_api_key=" "))


def test_anthropic_models_are_built_with_the_configured_names(tmp_path: Path) -> None:
    from langchain_anthropic import ChatAnthropic

    models = build_chat_models(
        settings_for(
            tmp_path,
            llm_provider="anthropic",
            anthropic_api_key="dummy-key",
            llm_model="some-future-sonnet",
            llm_fast_model="some-future-haiku",
        )
    )
    assert isinstance(models.main, ChatAnthropic) and isinstance(models.fast, ChatAnthropic)
    assert models.main.model == "some-future-sonnet" and models.fast.model == "some-future-haiku"
    assert models.main_name == "some-future-sonnet" and models.provider == "anthropic"


def test_gemini_models_are_built_and_share_google_api_key(tmp_path: Path) -> None:
    from langchain_google_genai import ChatGoogleGenerativeAI

    models = build_chat_models(
        settings_for(
            tmp_path,
            llm_provider="gemini",
            google_api_key="dummy-key",
            llm_model="gemini-2.5-flash",
            llm_fast_model="gemini-2.5-flash-lite",
        )
    )
    assert isinstance(models.main, ChatGoogleGenerativeAI)
    assert getattr(models.main, "model", "").endswith("gemini-2.5-flash")
    assert getattr(models.fast, "model", "").endswith("gemini-2.5-flash-lite")


def test_fake_provider_needs_no_key(tmp_path: Path) -> None:
    models = build_chat_models(settings_for(tmp_path, llm_provider="fake"))
    assert isinstance(models.main, FakeChatModel) and models.provider == "fake"


async def test_fake_model_streams_words_with_usage_and_records_calls() -> None:
    model = FakeChatModel(responder=lambda messages: "alpha beta gamma")
    pieces: list[AIMessageChunk] = []
    async for piece in model.astream([SystemMessage(content="s"), HumanMessage(content="q one")]):
        pieces.append(piece)
    assert "".join(p.text for p in pieces) == "alpha beta gamma"
    assert len(pieces) > 2  # streamed in several chunks
    total = pieces[0]
    for piece in pieces[1:]:
        total = total + piece
    assert total.usage_metadata is not None and total.usage_metadata["output_tokens"] == 3
    assert len(model.calls) == 1 and model.calls[0][1].content == "q one"

    reply = await model.ainvoke([HumanMessage(content="again")])
    assert reply.content == "alpha beta gamma"
    assert len(model.calls) == 2


async def test_fake_model_can_fail_and_stall() -> None:
    failing = FakeChatModel(error=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        await failing.ainvoke([HumanMessage(content="x")])
    slow = FakeChatModel(responder=lambda m: "a b c d", delay_s=0.3)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(slow.ainvoke([HumanMessage(content="x")]), 0.05)


def test_importing_the_factory_does_not_import_provider_packages() -> None:
    import subprocess
    import sys

    code = (
        "import sys, app.llm.factory;"
        "bad = [m for m in ('langchain_anthropic', 'langchain_google_genai') if m in sys.modules];"
        "sys.exit(1 if bad else 0)"
    )
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0
