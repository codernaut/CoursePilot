"""LangChain / LangSmith tracing policy (CLAUDE.md section 13, personal data).

Tracing sends prompts, answers and excerpts of the uploaded material to a third party, so it is OFF
unless ``LANGSMITH_API_KEY`` is set deliberately in the settings. Because LangChain also switches
tracing on from plain environment variables (``LANGCHAIN_TRACING_V2``, ``LANGSMITH_TRACING``...),
which a developer shell or a hosting platform may carry, ``apply_tracing_policy`` runs at startup
(web app and worker) and forces those variables to ``false`` whenever the key is not configured.
"""

import os

from app.core.config import Settings
from app.core.logging import get_logger

log = get_logger(__name__)

TRACING_FLAGS = (
    "LANGCHAIN_TRACING_V2",
    "LANGCHAIN_TRACING",
    "LANGSMITH_TRACING",
    "LANGSMITH_TRACING_V2",
)


def apply_tracing_policy(settings: Settings) -> bool:
    """Force the tracing flags according to the settings. Returns True when tracing is enabled."""
    key = settings.langsmith_api_key
    if key is None or not key.get_secret_value().strip():
        for name in TRACING_FLAGS:
            os.environ[name] = "false"
        return False
    os.environ["LANGSMITH_API_KEY"] = key.get_secret_value()
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    log.warning(
        "langsmith_tracing_enabled",
        sends="prompts, answers, retrieved material excerpts and web results of every chat turn",
        to="LangSmith (a third party)",
        disable="remove LANGSMITH_API_KEY from .env and restart",
    )
    return True
