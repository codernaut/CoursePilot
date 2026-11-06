"""Interactive first-time setup (``python -m app.cli setup``).

All terminal I/O goes through a ``Prompter`` so the flow is unit-testable. ``.env`` handling is in
``EnvFile``: it preserves comments and unknown lines, never prints secrets and never rewrites a
good ``SECRET_KEY``.
"""

import os
import re
import secrets
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from app.core.config import INSECURE_SECRET_KEYS, MIN_PRODUCTION_SECRET_KEY_LENGTH, Settings
from app.core.errors import AppError
from app.services.password_policy import validate_password

MIN_PYTHON = (3, 12)

LLM_DEFAULTS: dict[str, tuple[str, str]] = {
    "anthropic": ("claude-sonnet-5-5", "claude-haiku-5-5"),
    "gemini": ("gemini-2.5-flash", "gemini-2.5-flash"),
}
LLM_KEY_VARS = {"anthropic": "ANTHROPIC_API_KEY", "gemini": "GOOGLE_API_KEY"}
EMBEDDING_DEFAULTS: dict[str, tuple[str, int]] = {
    "gemini": ("gemini-embedding-001", 768),
    "voyage": ("voyage-3.5", 1024),
    "openai": ("text-embedding-3-small", 1536),
}
EMBEDDING_KEY_VARS = {
    "gemini": "GOOGLE_API_KEY",
    "voyage": "VOYAGE_API_KEY",
    "openai": "OPENAI_API_KEY",
}

_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_COMMENT_RE = re.compile(r"^(.*?)(\s+#.*)?$")


class Prompter(Protocol):
    def say(self, text: str) -> None: ...

    def ask(self, prompt: str) -> str:
        """Visible input. Returns the entered text (empty string for Enter)."""

    def ask_secret(self, prompt: str) -> str:
        """Hidden input (passwords, API keys)."""


class ConsolePrompter:
    def say(self, text: str) -> None:
        print(text)

    def ask(self, prompt: str) -> str:
        return input(f"{prompt} ")

    def ask_secret(self, prompt: str) -> str:
        import getpass

        return getpass.getpass(f"{prompt} ")


def mask(value: str | None) -> str:
    if not value:
        return "(not set)"
    return f"****{value[-4:]}" if len(value) >= 12 else "****"


def _split_value(rest: str) -> tuple[str, str]:
    """Split the text after ``KEY=`` into (value, trailing comment), honouring quotes."""
    raw = rest.strip()
    if raw[:1] in ('"', "'") and len(raw) > 1:
        quote = raw[0]
        out: list[str] = []
        i = 1
        while i < len(raw):
            char = raw[i]
            if quote == '"' and char == "\\" and i + 1 < len(raw):
                out.append(raw[i + 1])
                i += 2
                continue
            if char == quote:
                tail = raw[i + 1 :].strip()
                return "".join(out), tail if tail.startswith("#") else ""
            out.append(char)
            i += 1
    match = _COMMENT_RE.match(raw)
    if match is None:  # pragma: no cover - the pattern matches every string
        return raw, ""
    return match.group(1).strip(), (match.group(2) or "").strip()


def _format_value(value: str) -> str:
    if value == "" or not re.search(r"[\s#\"']", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class EnvFile:
    """A .env file held as lines; supports reading and updating single keys in place."""

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines

    @classmethod
    def load(cls, env_path: Path, example_path: Path) -> "EnvFile":
        source = env_path if env_path.exists() else example_path
        text = source.read_text(encoding="utf-8") if source.exists() else ""
        return cls(text.splitlines())

    def get(self, key: str) -> str | None:
        for line in self.lines:
            match = _LINE_RE.match(line)
            if match and match.group(1) == key:
                return _split_value(match.group(2))[0]
        return None

    def set(self, key: str, value: str) -> None:
        formatted = _format_value(value)
        for index, line in enumerate(self.lines):
            match = _LINE_RE.match(line)
            if match and match.group(1) == key:
                comment = _split_value(match.group(2))[1]
                if comment:
                    self.lines[index] = f"{key}={formatted}".ljust(36) + comment
                else:
                    self.lines[index] = f"{key}={formatted}"
                return
        self.lines.append(f"{key}={formatted}")

    def write(self, path: Path) -> None:
        # The file holds SECRET_KEY and API keys: create it owner-only (no world-readable window).
        data = ("\n".join(self.lines) + "\n").encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(path, 0o600)  # also tightens a pre-existing, more permissive file


@dataclass
class SetupDeps:
    """Seams for the parts of setup that touch the database (patched in unit tests)."""

    migrate: Callable[[Settings], None]
    seed: Callable[[Settings], int]
    admin_exists: Callable[[Settings], bool]
    create_admin: Callable[[Settings, str, str, str], None]
    check_sqlite: Callable[[], list[str]]


def _ask_default(p: Prompter, prompt: str, default: str) -> str:
    answer = p.ask(f"{prompt} [{default}]:").strip()
    return answer or default


def _ask_choice(p: Prompter, prompt: str, choices: list[str], default: str) -> str:
    while True:
        answer = _ask_default(p, f"{prompt} ({'/'.join(choices)})", default).lower()
        if answer in choices:
            return answer
        p.say(f"Please choose one of: {', '.join(choices)}.")


def _ask_port(p: Prompter, default: str) -> str:
    while True:
        answer = _ask_default(p, "Port for the web app", default)
        if answer.isascii() and answer.isdigit() and 1 <= int(answer) <= 65535:
            return answer
        p.say("Enter a port number between 1 and 65535.")


_TRUE_VALUES = {"true", "1", "yes", "on"}
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "::"}  # noqa: S104
LAN_WARNING = (
    "Warning: without HTTPS, passwords and session cookies cross the network in clear text. "
    "Anyone on the same network can read them. Use this only on a closed network you trust."
)


def parse_lan_base_url(value: str) -> str | None:
    """A normalised ``http(s)://host[:port]`` URL others can reach, or None if it is unusable."""
    try:
        parts = urlsplit(value.strip())
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError for a malformed port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host or host in _LOOPBACK_HOSTS:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def _ask_yes_no(p: Prompter, prompt: str, default: bool) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        answer = p.ask(f"{prompt} [{hint}]:").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        p.say("Please answer y or n.")


def _ask_lan_base_url(p: Prompter, current: str, port: str) -> str:
    suggestion = current if parse_lan_base_url(current) else ""
    example = f"http://192.168.1.20:{port}"
    for _ in range(5):
        shown = suggestion or example
        answer = p.ask(f"Address other computers will type in their browser [{shown}]:").strip()
        candidate = parse_lan_base_url(answer or suggestion)
        if candidate:
            return candidate
        p.say(
            "Enter the machine's network address as http://HOST:PORT (not localhost), "
            f"for example {example}."
        )
    p.say(f"Using {example}. Edit BASE_URL in .env if that is wrong.")
    return example


def _ask_key(p: Prompter, env: EnvFile, var: str, label: str) -> None:
    """Hidden prompt: Enter keeps the current value, '-' clears it, anything else replaces it."""
    current = env.get(var) or ""
    answer = p.ask_secret(
        f"{label} [{mask(current)}] (Enter keeps it, '-' clears it, empty skips):"
    ).strip()
    if answer == "-":
        env.set(var, "")
    elif answer:
        env.set(var, answer)


def run_setup(
    p: Prompter,
    deps: SetupDeps,
    *,
    env_path: Path,
    example_path: Path,
    python_version: tuple[int, int] | None = None,
) -> int:
    version = python_version or (sys.version_info.major, sys.version_info.minor)
    if version < MIN_PYTHON:
        needed = f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}"
        p.say(f"Python {needed} or newer is required (found {version[0]}.{version[1]}).")
        return 1
    problems = deps.check_sqlite()
    if problems:
        p.say("This computer cannot run CoursePilot yet:")
        for problem in problems:
            p.say(f"  - {problem}")
        return 1

    p.say("CoursePilot setup. Press Enter to accept the value in [brackets].")
    env = EnvFile.load(env_path, example_path)

    # Web app address
    old_port = env.get("APP_PORT") or "8000"
    port = _ask_port(p, old_port)
    env.set("APP_PORT", port)
    base_url = env.get("BASE_URL") or ""

    # Network exposure: loopback only (default) or the whole LAN without TLS.
    was_lan = (env.get("ALLOW_INSECURE_HTTP") or "").lower() in _TRUE_VALUES
    p.say(
        "Current network setting: "
        + ("other computers may connect without HTTPS." if was_lan else "this computer only.")
    )
    lan = _ask_yes_no(
        p,
        "Will other computers connect to this machine over the network without HTTPS?",
        was_lan,
    )
    if lan:
        p.say(LAN_WARNING)
        env.set("APP_HOST", "0.0.0.0")  # noqa: S104 - the owner asked for LAN access
        env.set("ALLOW_INSECURE_HTTP", "true")
        env.set("BASE_URL", _ask_lan_base_url(p, base_url, port))
    else:
        # Only undo what the LAN answer set; a host the owner chose (e.g. 0.0.0.0 behind a TLS
        # proxy on another machine) is left alone.
        if was_lan or not env.get("APP_HOST"):
            env.set("APP_HOST", "127.0.0.1")
        env.set("ALLOW_INSECURE_HTTP", "false")
        if env.get("APP_ENV") != "production" and (
            was_lan or not base_url or base_url.startswith(("http://localhost", "http://127.0.0.1"))
        ):
            env.set("BASE_URL", f"http://localhost:{port}")

    # Session-signing secret: generated once, never replaced when it is already strong.
    current_secret = env.get("SECRET_KEY") or ""
    if (
        current_secret.strip().lower() in INSECURE_SECRET_KEYS
        or len(current_secret) < MIN_PRODUCTION_SECRET_KEY_LENGTH
    ):
        env.set("SECRET_KEY", secrets.token_urlsafe(48))
        p.say("Generated a new SECRET_KEY.")

    # Chat model
    old_provider = env.get("LLM_PROVIDER") or "anthropic"
    provider = _ask_choice(p, "Chat AI provider", list(LLM_DEFAULTS), old_provider)
    default_model, default_fast = LLM_DEFAULTS[provider]
    keep = provider == old_provider
    env.set("LLM_PROVIDER", provider)
    env.set(
        "LLM_MODEL",
        _ask_default(p, "Chat model", (env.get("LLM_MODEL") if keep else None) or default_model),
    )
    env.set(
        "LLM_FAST_MODEL",
        _ask_default(
            p, "Fast model", (env.get("LLM_FAST_MODEL") if keep else None) or default_fast
        ),
    )
    asked_keys: set[str] = set()
    llm_key = LLM_KEY_VARS[provider]
    _ask_key(p, env, llm_key, f"{provider.capitalize()} API key (optional)")
    asked_keys.add(llm_key)

    # Embeddings
    p.say("Embeddings are independent of the chat model (Anthropic has no embeddings API).")
    old_embedding = env.get("EMBEDDING_PROVIDER") or "gemini"
    old_embedding_cfg = (old_embedding, env.get("EMBEDDING_MODEL"), env.get("EMBEDDING_DIM"))
    embedding = _ask_choice(p, "Embedding provider", list(EMBEDDING_DEFAULTS), old_embedding)
    emb_model, emb_dim = EMBEDDING_DEFAULTS[embedding]
    same = embedding == old_embedding
    env.set("EMBEDDING_PROVIDER", embedding)
    env.set(
        "EMBEDDING_MODEL",
        _ask_default(
            p, "Embedding model", (env.get("EMBEDDING_MODEL") if same else None) or emb_model
        ),
    )
    env.set(
        "EMBEDDING_DIM",
        _ask_default(
            p, "Embedding dimension", (env.get("EMBEDDING_DIM") if same else None) or str(emb_dim)
        ),
    )
    emb_key = EMBEDDING_KEY_VARS[embedding]
    if emb_key not in asked_keys:
        _ask_key(p, env, emb_key, f"{embedding.capitalize()} API key (optional)")
        asked_keys.add(emb_key)
    if (embedding, env.get("EMBEDDING_MODEL"), env.get("EMBEDDING_DIM")) != old_embedding_cfg:
        p.say(
            "Note: changing the embedding settings after material was processed needs "
            "'python -m app.cli reindex-all'."
        )

    # Web search
    _ask_key(p, env, "TAVILY_API_KEY", "Tavily web search API key (optional)")

    env.write(env_path)
    p.say(f"Wrote {env_path}.")

    settings = Settings(_env_file=env_path)
    deps.migrate(settings)
    p.say("Database is up to date.")
    created = deps.seed(settings)
    p.say(f"Programme structure ready ({created} new rows).")

    if deps.admin_exists(settings):
        p.say("An administrator account already exists.")
    else:
        p.say("Create the first administrator account.")
        _create_admin_interactively(p, deps, settings)

    p.say("Setup complete. Start CoursePilot with start.bat (Windows) or ./start.sh.")
    return 0


def prompt_admin_details(p: Prompter, email: str | None = None) -> tuple[str, str, str] | None:
    """Ask for email, display name and a confirmed password (hidden). None if it gives up."""
    email = (email or p.ask("Admin email:")).strip()
    name = p.ask("Display name [Administrator]:").strip() or "Administrator"
    for _ in range(3):
        password = p.ask_secret("Password (min 10 characters, hidden):")
        try:
            validate_password(password, email=email)
        except AppError as exc:
            p.say(exc.message)
            continue
        if password != p.ask_secret("Repeat password:"):
            p.say("The passwords do not match.")
            continue
        return email, name, password
    p.say("Giving up after 3 attempts.")
    return None


def _create_admin_interactively(p: Prompter, deps: SetupDeps, settings: Settings) -> None:
    details = prompt_admin_details(p)
    if details is None:
        p.say("No administrator was created. Run 'python -m app.cli create-admin' later.")
        return
    email, name, password = details
    try:
        deps.create_admin(settings, email, name, password)
    except Exception as exc:  # readable message, never the password
        p.say(f"Could not create the administrator: {getattr(exc, 'message', exc)}")
        p.say("Run 'python -m app.cli create-admin' to try again.")
        return
    p.say(f"Created administrator {email.strip().lower()}.")
