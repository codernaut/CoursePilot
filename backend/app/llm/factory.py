"""The chat-model factory: the only place that knows which provider is configured.

``build_chat_models`` returns LangChain chat models created through ``init_chat_model`` for the
configured provider and raises the typed ``ProviderNotConfigured`` when a key, address or model
is missing. Providers: ``anthropic`` and ``gemini`` (hosted, ``init_chat_model``), ``ollama``
(``ChatOllama``), ``openai_compatible`` (``ChatOpenAI`` with a ``base_url``: vLLM, LM Studio,
llama.cpp server, LocalAI, Ollama's ``/v1``) and ``fake`` (tests only). Creating a model opens no
connection and the provider packages are imported only here, on first use. Model names come from
settings (``LLM_MODEL`` for answers, ``LLM_FAST_MODEL`` for query rewriting and sufficiency
checks); no catalogue is hardcoded here. The Anthropic-only options (effort, prompt caching) never
reach the local providers.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import SecretStr

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
_PROVIDERS = {  # hosted providers only
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


_FOREIGN_MODEL_PREFIXES = ("claude", "gemini")  # a hosted default left behind after a switch
LOCAL_KEEP_ALIVE = "30m"  # Ollama keeps the model in memory between questions


def _check_local_model(settings: Settings, model: str, var: str) -> None:
    if not model.strip() or model.lower().startswith(_FOREIGN_MODEL_PREFIXES):
        raise ProviderNotConfigured(
            f"{var} is '{model}', which is not a local model. Set {var} in .env to a model your "
            f"{settings.llm_provider} server serves (for Ollama, one you pulled with "
            "'ollama pull') and restart CoursePilot."
        )


def _build_local(settings: Settings) -> ChatModels:
    """``ollama`` and ``openai_compatible``: no API key needed, but an address and a model."""
    for model, var in (
        (settings.llm_model, "LLM_MODEL"),
        (settings.llm_fast_model, "LLM_FAST_MODEL"),
    ):
        _check_local_model(settings, model, var)
    timeout = float(settings.agent_timeout_s)

    if settings.llm_provider == "ollama":
        from langchain_ollama import ChatOllama

        def make(model: str, *, max_tokens: int, temperature: float) -> BaseChatModel:
            return ChatOllama(
                model=model,
                base_url=settings.ollama_base_url,
                num_predict=max_tokens,
                temperature=temperature,
                keep_alive=LOCAL_KEEP_ALIVE,
                client_kwargs={"timeout": timeout},
            )

    else:
        if not settings.local_llm_base_url:
            raise ProviderNotConfigured(
                "the openai_compatible chat provider needs LOCAL_LLM_BASE_URL in .env "
                "(for example http://localhost:8001/v1). Set it and restart CoursePilot."
            )
        from langchain_openai import ChatOpenAI

        secret = settings.local_llm_api_key
        key = secret.get_secret_value().strip() if secret else ""

        def make(model: str, *, max_tokens: int, temperature: float) -> BaseChatModel:
            return ChatOpenAI(
                model=model,
                base_url=settings.local_llm_base_url,
                api_key=SecretStr(key or "not-needed"),  # many local servers ignore the key
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=timeout,
                max_retries=MAX_RETRIES,
            )

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
    if settings.llm_provider in ("ollama", "openai_compatible"):
        return _build_local(settings)
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
        if settings.llm_provider == "anthropic":
            # Current Claude models (Sonnet 5.5, Haiku 5.5, Opus 5.5...) reject any non-default
            # sampling parameter with a 400, so no ``temperature`` is sent. They think
            # adaptively by default and thinking counts toward ``max_tokens``; ``effort`` keeps
            # that thinking small for these short, grounded answers.
            if settings.llm_effort:
                options["output_config"] = {"effort": settings.llm_effort}
        else:
            options["temperature"] = temperature
        built: BaseChatModel = init_chat_model(
            model,
            model_provider=provider_id,
            api_key=key,
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
