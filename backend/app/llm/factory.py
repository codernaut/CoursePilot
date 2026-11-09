"""The chat-model factory: the only place that knows which provider is configured.

``build_chat_models`` returns LangChain chat models created through ``init_chat_model`` for the
configured provider (``anthropic`` or ``gemini``; ``fake`` only for tests) and raises the typed
``ProviderNotConfigured`` when the API key is missing. Creating a model opens no connection and
the provider packages are imported only here, on first use. Model names come from settings
(``LLM_MODEL`` for answers, ``LLM_FAST_MODEL`` for query rewriting and sufficiency checks); no
catalogue is hardcoded here. A local provider (Ollama) later means one more branch.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from app.core.config import Settings

MAX_RETRIES = 1


class ProviderNotConfigured(Exception):  # noqa: N818 - the name is part of the spec
    """No API key for the configured chat provider. ``hint`` says which setting is missing (shown
    to administrators only)."""

    def __init__(self, hint: str) -> None:
        super().__init__(hint)
        self.hint = hint


@dataclass(frozen=True)
class ChatModels:
    main: BaseChatModel
    fast: BaseChatModel
    main_name: str
    fast_name: str
    provider: str


LLMFactory = Callable[[], ChatModels]

# provider -> (init_chat_model provider id, env var, label)
_PROVIDERS = {
    "anthropic": ("anthropic", "ANTHROPIC_API_KEY", "Anthropic"),
    "gemini": ("google_genai", "GOOGLE_API_KEY", "Gemini"),
}


def _api_key(settings: Settings) -> str:
    secret = (
        settings.google_api_key if settings.llm_provider == "gemini" else settings.anthropic_api_key
    )
    _, var, label = _PROVIDERS[settings.llm_provider]
    key = secret.get_secret_value().strip() if secret else ""
    if not key:
        raise ProviderNotConfigured(
            f"the {label} chat provider needs {var} in .env. Set it and restart CoursePilot."
        )
    return key


def build_chat_models(settings: Settings) -> ChatModels:
    """The main and fast models for the configured provider (cheap: no network)."""
    if settings.llm_provider == "fake":
        from app.llm.fake import FakeChatModel

        return ChatModels(
            main=FakeChatModel(model_name=settings.llm_model),
            fast=FakeChatModel(model_name=settings.llm_fast_model),
            main_name=settings.llm_model,
            fast_name=settings.llm_fast_model,
            provider="fake",
        )
    key = _api_key(settings)
    from langchain.chat_models import init_chat_model

    provider_id = _PROVIDERS[settings.llm_provider][0]
    timeout = float(settings.agent_timeout_s)

    # The output-length parameter has a different name per provider: langchain-anthropic takes
    # ``max_tokens``; langchain-google-genai's field is ``max_output_tokens`` (``max_tokens`` is
    # only an alias there).
    limit_name = "max_output_tokens" if settings.llm_provider == "gemini" else "max_tokens"

    def make(model: str, *, max_tokens: int, temperature: float) -> BaseChatModel:
        options: dict[str, Any] = {limit_name: max_tokens}
        built: BaseChatModel = init_chat_model(
            model,
            model_provider=provider_id,
            api_key=key,
            temperature=temperature,
            timeout=timeout,
            max_retries=MAX_RETRIES,
            **options,
        )
        return built

    return ChatModels(
        main=make(settings.llm_model, max_tokens=settings.llm_max_output_tokens, temperature=0.2),
        fast=make(
            settings.llm_fast_model,
            max_tokens=settings.llm_fast_max_output_tokens,
            temperature=0.0,
        ),
        main_name=settings.llm_model,
        fast_name=settings.llm_fast_model,
        provider=settings.llm_provider,
    )
