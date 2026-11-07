"""Structured logging (structlog): JSON lines in production, a console renderer in development.

* Every record goes through a redaction processor (a safety net: known secret shapes and fields
  with secret-looking names are masked). The real rule is to never log prompts, answers, passwords,
  tokens or keys in the first place.
* Records from the standard ``logging`` module (uvicorn, SQLAlchemy, httpx...) are rendered by the
  same pipeline, so the output is one consistent stream.
* ``LOG_LEVEL`` is honoured by both.
"""

import logging
import re
import sys
from typing import Any, TextIO

import structlog

REDACTED = "[REDACTED]"

# Field names whose values are never logged (matched on the lowercased key).
_SECRET_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "prompt",
    "answer",
)

# Shapes of secrets that may appear inside a string: provider keys, bearer tokens, PEM blocks and
# password hashes.
_SECRET_PATTERNS = (
    # SQLAlchemy error text: "[parameters: (...)]" holds the bound values (chat messages, document
    # text, e-mails). repr() keeps it on one line.
    re.compile(r"(?<=\[parameters: ).*"),
    re.compile(r"(?<![A-Za-z0-9])sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"tvly-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?<![A-Za-z0-9])pa-[A-Za-z0-9_\-]{24,}"),  # Voyage
    re.compile(r"-----BEGIN [A-Z ]+-----[\s\S]*?(?:-----END [A-Z ]+-----|$)"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{8,}"),
    re.compile(r"\$argon2[a-z]*\$[^\s\"']+"),
    # An Authorization header: the scheme word (Basic, Bearer...) is not the secret, its value is.
    re.compile(
        r"(?i)(?<![a-z0-9])(authorization[\"']?)(\s*[=:]\s*[\"']?)"
        r"(?:(?:basic|bearer|digest|token)\s+)?[^\s,;\"']{4,}"
    ),
    # Cookie / Set-Cookie header lines: everything up to the end of the line.
    re.compile(r"(?i)(?<![a-z0-9])((?:set-)?cookie[\"']?)(\s*[=:]\s*[\"']?)[^\r\n\"']{4,}"),
    # name=value / name: value pairs whose (possibly prefixed or suffixed, e.g. client_secret,
    # access_token) name looks secret; quoted values included. The affixes are length-bounded so
    # a long run of word characters cannot make matching quadratic.
    re.compile(
        r"(?i)(?<![a-z0-9])((?:[a-z0-9]{1,20}[_-]){0,3}"
        r"(?:api[_-]?key|token|password|passwd|secret)(?:[_-][a-z0-9]{1,20}){0,3}[\"']?)"
        r"(\s*[=:]\s*[\"']?)[^\s,;\"'&]{4,}(?: [^\s,;\"'&]+(?=[\"']))?"
    ),
)


def _redact_string(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
        else:
            text = pattern.sub(REDACTED, text)
    return text


def redact_text(text: str) -> str:
    """Mask secret shapes in free text (used where stored error text is shown to administrators)."""
    return _redact_string(text)


def _redact_value(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        return _redact_string(value)
    if depth >= 4:
        return value
    if isinstance(value, dict):
        return {k: _redact_field(k, v, depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact_value(v, depth + 1) for v in value]
    return value


def _redact_field(key: object, value: Any, depth: int = 0) -> Any:
    if (
        isinstance(key, str)
        and isinstance(value, str | bytes)
        and value
        and any(part in key.lower() for part in _SECRET_KEY_PARTS)
    ):
        return REDACTED
    return _redact_value(value, depth)


def redact_processor(
    logger: Any, method_name: str, event_dict: structlog.typing.EventDict
) -> structlog.typing.EventDict:
    """structlog processor: mask secret-looking fields and secret shapes inside strings."""
    for key in list(event_dict):
        if key in ("event", "level", "timestamp"):
            event_dict[key] = _redact_value(event_dict[key])
        else:
            event_dict[key] = _redact_field(key, event_dict[key])
    return event_dict


_HANDLER_TAG = "_coursepilot_handler"
# Libraries whose INFO/DEBUG records can carry URLs, headers, SQL parameters (aiosqlite logs every
# statement with its parameters, i.e. whole messages and documents) or prompts. They stay at
# WARNING whatever LOG_LEVEL says.
_QUIET_LOGGERS = (
    "aiosqlite",
    "sqlalchemy.engine",
    "sqlalchemy.pool",
    "httpx",
    "httpcore",
    "python_multipart",
    "multipart",
    "langchain",
    "langchain_core",
    "langgraph",
    "langsmith",
    "anthropic",
    "google_genai",
    "google.genai",
    "urllib3",
)


def configure_logging(
    level: str = "INFO", *, json: bool = True, stream: TextIO | None = None
) -> None:
    log_level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    )
    shared: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        redact_processor,
    ]
    target = stream if stream is not None else sys.stdout
    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(file=target),
        # Not cached, so reconfiguring (tests, a changed level) takes effect everywhere.
        cache_logger_on_first_use=False,
    )

    # Standard-library records (uvicorn, sqlalchemy, httpx...) use the same pipeline.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[
            structlog.stdlib.add_logger_name,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
        ],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            redact_processor,
            renderer,
        ],
    )
    handler = logging.StreamHandler(target)
    handler.setFormatter(formatter)
    setattr(handler, _HANDLER_TAG, True)
    root = logging.getLogger()
    for existing in list(root.handlers):  # only our own earlier handler; leave pytest's alone
        if getattr(existing, _HANDLER_TAG, False):
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(log_level)
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(max(log_level, logging.WARNING))
    # We write our own access log (app.core.middleware.AccessLogMiddleware).
    logging.getLogger("uvicorn.access").disabled = True


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)
