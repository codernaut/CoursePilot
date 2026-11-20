import sqlite3
from pathlib import Path

import pytest

from app.core.config import REPO_ROOT, Settings
from app.setup_flow import (
    OTHER_SETTINGS_HEADER,
    EnvFile,
    SetupDeps,
    mask,
    prompt_admin_details,
    render_from_template,
    run_setup,
    sync_env_file,
)

EXAMPLE = REPO_ROOT / ".env.example"
ANTHROPIC_KEY = "sk-ant-abcdefghij1234"
GOOGLE_KEY = "AIza-google-key-9876"


class Scripted:
    """A Prompter that replays answers in order and records every prompt and message."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.prompts: list[tuple[str, bool]] = []  # (prompt, secret?)
        self.said: list[str] = []

    def say(self, text: str) -> None:
        self.said.append(text)

    def _next(self, prompt: str, secret: bool) -> str:
        self.prompts.append((prompt, secret))
        assert self.answers, f"unexpected extra prompt: {prompt}"
        return self.answers.pop(0)

    def ask(self, prompt: str) -> str:
        return self._next(prompt, False)

    def ask_secret(self, prompt: str) -> str:
        return self._next(prompt, True)

    @property
    def everything_shown(self) -> str:
        return "\n".join(self.said + [p for p, _ in self.prompts])


class FakeDeps:
    def __init__(self, *, admin_exists: bool = False, sqlite_problems: list[str] | None = None):
        self.migrated: list[Settings] = []
        self.seeded: list[Settings] = []
        self.admins: list[tuple[str, str, str]] = []
        self._admin_exists = admin_exists
        self._problems = sqlite_problems or []

    def _seed(self, settings: Settings) -> int:
        self.seeded.append(settings)
        return 39

    def build(self) -> SetupDeps:
        return SetupDeps(
            migrate=self.migrated.append,
            seed=self._seed,
            admin_exists=lambda _s: self._admin_exists,
            create_admin=lambda _s, e, n, p: self.admins.append((e, n, p)),
            check_sqlite=lambda: self._problems,
        )


# Prompt order: port, "other computers without HTTPS?", provider, model, fast model, LLM key
# (secret), embedding provider, model, dim, [embedding key (secret) when it is a different
# variable], Tavily key (secret).
FRESH_DEFAULTS = ["", "", "", "", "", ANTHROPIC_KEY, "", "", "", GOOGLE_KEY, ""]
ADMIN_ANSWERS = ["admin@example.edu", "", "very secret pw 1", "very secret pw 1"]


def _run(tmp_path: Path, answers: list[str], deps: FakeDeps | None = None, **kwargs):  # type: ignore[no-untyped-def]
    deps = deps or FakeDeps()
    p = Scripted(answers)
    env_path = tmp_path / ".env"
    code = run_setup(p, deps.build(), env_path=env_path, example_path=EXAMPLE, **kwargs)
    return code, p, deps, env_path


def test_mask() -> None:
    assert mask(None) == "(not set)"
    assert mask("") == "(not set)"
    assert mask("short") == "****"
    assert mask("sk-ant-abcdefghij1234") == "****1234"
    assert "abcdefghij" not in mask("sk-ant-abcdefghij1234")


def test_setup_explains_that_web_search_is_optional_and_query_only(tmp_path: Path) -> None:
    code, p, _, env_path = _run(tmp_path, ["9001", *FRESH_DEFAULTS[1:], *ADMIN_ANSWERS])
    assert code == 0
    shown = p.everything_shown
    assert "Web search (optional)" in shown and "Only the search query is sent" in shown
    assert "not configured" in shown
    assert EnvFile.load(env_path, EXAMPLE).get("WEB_SEARCH_PROVIDER") == "tavily"


def test_fresh_setup_writes_env_generates_secret_and_creates_admin(tmp_path: Path) -> None:
    code, p, deps, env_path = _run(tmp_path, ["9001", *FRESH_DEFAULTS[1:], *ADMIN_ANSWERS])
    assert code == 0
    env = EnvFile.load(env_path, EXAMPLE)
    assert env.get("APP_PORT") == "9001"
    assert env.get("BASE_URL") == "http://localhost:9001"
    secret = env.get("SECRET_KEY") or ""
    assert secret != "change-me" and len(secret) >= 32
    assert env.get("LLM_PROVIDER") == "anthropic"
    assert env.get("LLM_MODEL") == "claude-sonnet-5-5"
    assert env.get("ANTHROPIC_API_KEY") == ANTHROPIC_KEY
    assert env.get("GOOGLE_API_KEY") == GOOGLE_KEY
    assert env.get("TAVILY_API_KEY") == ""
    assert env.get("EMBEDDING_PROVIDER") == "gemini"
    assert env.get("EMBEDDING_DIM") == "768"
    # The documented structure and comments survive.
    text = env_path.read_text(encoding="utf-8")
    assert "# ===== Server and security =====" in text and "Options: true | false" in text
    assert "PROGRAM_YEARS=3" in text
    # The file still loads as valid Settings.
    settings = Settings(_env_file=env_path)
    assert settings.app_port == 9001
    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == ANTHROPIC_KEY
    # Steps ran in order and the admin was created with the hidden-prompt password.
    assert len(deps.migrated) == 1 and len(deps.seeded) == 1
    assert deps.admins == [("admin@example.edu", "Administrator", "very secret pw 1")]
    # Nothing secret was ever displayed.
    shown = p.everything_shown
    for secret_value in (ANTHROPIC_KEY, GOOGLE_KEY, "very secret pw 1", secret):
        assert secret_value not in shown


def test_secrets_and_passwords_use_hidden_prompts(tmp_path: Path) -> None:
    _, p, _, _ = _run(tmp_path, ["", *FRESH_DEFAULTS[1:], *ADMIN_ANSWERS])
    secret_prompts = [prompt for prompt, secret in p.prompts if secret]
    assert any("Anthropic API key" in s for s in secret_prompts)
    assert any("Tavily" in s for s in secret_prompts)
    assert any(s.startswith("Password") for s in secret_prompts)
    assert any(s.startswith("Repeat password") for s in secret_prompts)
    assert not any("key" in prompt.lower() for prompt, secret in p.prompts if not secret)


def test_rerun_masks_existing_values_and_enter_keeps_them(tmp_path: Path) -> None:
    _run(tmp_path, ["9001", *FRESH_DEFAULTS[1:], *ADMIN_ANSWERS])
    env_path = tmp_path / ".env"
    first_secret = EnvFile.load(env_path, EXAMPLE).get("SECRET_KEY")
    answers = [""] * 11  # Enter everywhere
    code, p, deps, _ = _run(tmp_path, answers, FakeDeps(admin_exists=True))
    assert code == 0
    env = EnvFile.load(env_path, EXAMPLE)
    assert env.get("SECRET_KEY") == first_secret  # a strong key is never regenerated
    assert env.get("ANTHROPIC_API_KEY") == ANTHROPIC_KEY
    assert env.get("GOOGLE_API_KEY") == GOOGLE_KEY
    assert env.get("APP_PORT") == "9001"
    prompts = " ".join(prompt for prompt, _ in p.prompts)
    assert "[****1234]" in prompts  # masked tail of the Anthropic key
    assert ANTHROPIC_KEY not in p.everything_shown and GOOGLE_KEY not in p.everything_shown
    assert deps.admins == []  # an admin already exists, nobody is asked


def test_dash_clears_a_key(tmp_path: Path) -> None:
    _run(tmp_path, ["", *FRESH_DEFAULTS[1:], *ADMIN_ANSWERS])
    answers = ["", "", "", "", "", "-", "", "", "", "", ""]
    _run(tmp_path, answers, FakeDeps(admin_exists=True))
    env = EnvFile.load(tmp_path / ".env", EXAMPLE)
    assert env.get("ANTHROPIC_API_KEY") == ""
    assert env.get("GOOGLE_API_KEY") == GOOGLE_KEY


def test_keys_can_be_skipped(tmp_path: Path) -> None:
    answers = [""] * 11 + ADMIN_ANSWERS
    code, _, _, env_path = _run(tmp_path, answers)
    assert code == 0
    settings = Settings(_env_file=env_path)
    assert settings.anthropic_api_key is None
    assert settings.google_api_key is None
    assert settings.tavily_api_key is None


def test_switching_provider_prefills_matching_model_defaults(tmp_path: Path) -> None:
    # gemini chat + gemini embeddings: the Google key is only asked once.
    answers = ["", "", "gemini", "", "", GOOGLE_KEY, "", "", "", "", *ADMIN_ANSWERS]
    code, p, _, env_path = _run(tmp_path, answers)
    assert code == 0
    env = EnvFile.load(env_path, EXAMPLE)
    assert env.get("LLM_PROVIDER") == "gemini"
    assert env.get("LLM_MODEL") == "gemini-2.5-flash"
    assert env.get("LLM_FAST_MODEL") == "gemini-2.5-flash"
    assert env.get("GOOGLE_API_KEY") == GOOGLE_KEY
    assert sum("Google" in prompt or "Gemini" in prompt for prompt, s in p.prompts if s) == 1


def test_custom_model_is_kept_when_provider_unchanged(tmp_path: Path) -> None:
    answers = ["", "", "", "my-custom-model", "", "", "", "", "", "", "", *ADMIN_ANSWERS]
    _, _, _, env_path = _run(tmp_path, answers)
    assert EnvFile.load(env_path, EXAMPLE).get("LLM_MODEL") == "my-custom-model"
    again = [""] * 11
    _run(tmp_path, again, FakeDeps(admin_exists=True))
    assert EnvFile.load(env_path, EXAMPLE).get("LLM_MODEL") == "my-custom-model"


def test_embedding_provider_choice_sets_model_and_dimension(tmp_path: Path) -> None:
    # voyage embeddings need their own key
    answers = ["", "", "", "", "", "", "voyage", "", "", "voyage-key-123456", "", *ADMIN_ANSWERS]
    _, _, _, env_path = _run(tmp_path, answers)
    env = EnvFile.load(env_path, EXAMPLE)
    assert (env.get("EMBEDDING_MODEL"), env.get("EMBEDDING_DIM")) == ("voyage-3.5", "1024")
    assert env.get("VOYAGE_API_KEY") == "voyage-key-123456"


def test_invalid_answers_are_asked_again(tmp_path: Path) -> None:
    answers = [
        "99999",
        "abc",
        "8123",
        "maybe",
        "n",
        "nonsense",
        "anthropic",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        *ADMIN_ANSWERS,
    ]
    code, p, _, env_path = _run(tmp_path, answers)
    assert code == 0
    assert EnvFile.load(env_path, EXAMPLE).get("APP_PORT") == "8123"
    assert any("answer y or n" in s for s in p.said)
    assert any("between 1 and 65535" in s for s in p.said)
    assert any("Please choose" in s for s in p.said)


def test_unknown_lines_are_kept_and_comments_replaced_by_the_template(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("# my notes\nCUSTOM_THING=1\nAPP_PORT=8000  # keep this comment\n", "utf-8")
    code, _, _, _ = _run(tmp_path, ["9100", *FRESH_DEFAULTS[1:], *ADMIN_ANSWERS])
    assert code == 0
    text = env_path.read_text("utf-8")
    assert "CUSTOM_THING=1" in text and "APP_PORT=9100" in text
    assert "# my notes" not in text and "# keep this comment" not in text
    assert "# ===== Server and security =====" in text


def test_old_python_is_refused(tmp_path: Path) -> None:
    code, p, deps, env_path = _run(tmp_path, [], python_version=(3, 11))
    assert code == 1
    assert "3.12" in p.everything_shown
    assert not env_path.exists() and deps.migrated == []


def test_sqlite_problems_stop_setup_with_readable_message(tmp_path: Path) -> None:
    deps = FakeDeps(sqlite_problems=["This SQLite build has no FTS5 support."])
    code, p, _, env_path = _run(tmp_path, [], deps)
    assert code == 1
    assert "no FTS5 support" in p.everything_shown
    assert not env_path.exists()


def test_admin_prompt_retries_short_and_mismatched_passwords() -> None:
    p = Scripted(
        [
            "a@example.edu",
            "Ann",
            "short",
            "long enough pw 1",
            "different pw 12",
            "pw number two 1",
            "pw number two 1",
        ]
    )
    assert prompt_admin_details(p) == ("a@example.edu", "Ann", "pw number two 1")
    assert any("at least 10" in s for s in p.said)
    assert any("do not match" in s for s in p.said)


def test_admin_prompt_gives_up_after_three_attempts() -> None:
    p = Scripted(["a@example.edu", "", "x", "y", "z"])
    assert prompt_admin_details(p) is None


def test_envfile_quotes_values_that_need_it(tmp_path: Path) -> None:
    env = EnvFile(["A=1  # note", "B="])
    env.set("B", "has space # and hash")
    env.set("C", "plain")
    env.set("A", "2")
    path = tmp_path / "x.env"
    env.write(path)
    reloaded = EnvFile.load(path, EXAMPLE)
    assert reloaded.get("B") == "has space # and hash"
    assert reloaded.get("C") == "plain"
    assert reloaded.get("A") == "2"
    assert "# note" in path.read_text("utf-8")


def test_real_deps_end_to_end_with_temp_database(tmp_path: Path) -> None:
    """setup with the real migrate/seed/create-admin against a temp DB and temp .env."""
    from app.cli import default_setup_deps

    example = tmp_path / "example.env"
    base = EXAMPLE.read_text("utf-8")
    base = base.replace("DATA_DIR=./data", f"DATA_DIR={(tmp_path / 'data').as_posix()}")
    base = base.replace(
        "DATABASE_URL=sqlite+aiosqlite:///./data/coursepilot.db",
        f"DATABASE_URL=sqlite+aiosqlite:///{(tmp_path / 'data' / 'cp.db').as_posix()}",
    )
    example.write_text(base, "utf-8")
    env_path = tmp_path / ".env"
    p = Scripted(["", *FRESH_DEFAULTS[1:], *ADMIN_ANSWERS])
    code = run_setup(p, default_setup_deps(), env_path=env_path, example_path=example)
    assert code == 0, p.everything_shown
    conn = sqlite3.connect(tmp_path / "data" / "cp.db")
    assert conn.execute("SELECT count(*) FROM modules").fetchone() == (27,)
    row = conn.execute("SELECT email, role, must_change_password FROM users").fetchone()
    assert row == ("admin@example.edu", "admin", 0)
    # Running it again changes nothing and asks no admin questions.
    p2 = Scripted([""] * 11)
    assert run_setup(p2, default_setup_deps(), env_path=env_path, example_path=example) == 0
    assert conn.execute("SELECT count(*) FROM users").fetchone() == (1,)
    assert conn.execute("SELECT count(*) FROM modules").fetchone() == (27,)


@pytest.mark.parametrize("value", ["", "x"])
def test_envfile_get_missing_key(value: str) -> None:
    assert EnvFile([f"A={value}"]).get("MISSING") is None


def test_env_file_is_restricted_to_the_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The .env holds SECRET_KEY and API keys: it must not be created world-readable."""
    import os

    modes: list[int] = []
    real_chmod = os.chmod

    def spy(path: object, mode: int, **kw: object) -> None:
        modes.append(mode)
        real_chmod(path, mode, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "chmod", spy)
    target = tmp_path / ".env"
    EnvFile(["A=1"]).write(target)
    assert modes and all(m & 0o077 == 0 for m in modes)


def test_port_prompt_rejects_non_ascii_digits(tmp_path: Path) -> None:
    code, _p, _deps, env_path = _run(tmp_path, ["²", *FRESH_DEFAULTS, *ADMIN_ANSWERS])
    assert code == 0
    assert EnvFile.load(env_path, EXAMPLE).get("APP_PORT") == "8000"


NL = chr(10)


def _example_lines() -> list[str]:
    return EXAMPLE.read_text(encoding="utf-8").splitlines()


def _line(lines: list[str], key: str) -> str:
    return next(line for line in lines if line.startswith(key + "="))


def test_render_preserves_values_exactly_and_drops_old_comments() -> None:
    current = [
        "# my own comment",
        "LLM_PROVIDER=gemini   # which one",
        'LLM_MODEL="my model # 2"',
        "GOOGLE_API_KEY=AIza-keep-1234",
        "ANTHROPIC_API_KEY=",
        "APP_PORT=8123",
        "APP_PORT=9000",  # last occurrence wins
    ]
    out = render_from_template(_example_lines(), current)
    assert _line(out, "LLM_PROVIDER") == "LLM_PROVIDER=gemini"
    assert _line(out, "LLM_MODEL") == 'LLM_MODEL="my model # 2"'
    assert _line(out, "GOOGLE_API_KEY") == "GOOGLE_API_KEY=AIza-keep-1234"
    assert _line(out, "ANTHROPIC_API_KEY") == "ANTHROPIC_API_KEY="
    assert _line(out, "APP_PORT") == "APP_PORT=9000"
    assert "# my own comment" not in out
    assert not any("# which one" in line for line in out)
    # missing options get the template default; comments come from the template
    ex = _example_lines()
    assert _line(out, "CHAT_DAILY_LIMIT_PER_USER") == _line(ex, "CHAT_DAILY_LIMIT_PER_USER")
    assert out[0] == ex[0]
    assert out.count("LLM_PROVIDER=gemini") == 1


def test_render_keeps_unknown_and_stray_lines_at_the_end_and_is_idempotent() -> None:
    current = ["# c", "MY_EXTRA=1", "MY_EXTRA=2", "export WEIRD=3", "APP_PORT=8001"]
    out = render_from_template(_example_lines(), current)
    tail = out[out.index(OTHER_SETTINGS_HEADER) :]
    assert tail == [OTHER_SETTINGS_HEADER, "MY_EXTRA=2", "export WEIRD=3"]
    assert render_from_template(_example_lines(), out) == out


def test_render_of_the_example_is_the_example() -> None:
    assert render_from_template(_example_lines(), _example_lines()) == _example_lines()


def test_sync_adds_options_and_does_not_rewrite_an_up_to_date_file(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "LLM_PROVIDER=gemini" + chr(10) + "GOOGLE_API_KEY=keep-this-secret-1234" + chr(10),
        encoding="utf-8",
    )
    before = Settings(_env_file=env_path)
    added = sync_env_file(env_path, EXAMPLE)
    assert added is not None and "LLM_EFFORT" in added and "LLM_PROVIDER" not in added
    after = Settings(_env_file=env_path)
    assert after.model_dump() == before.model_dump()
    assert after.google_api_key is not None
    assert after.google_api_key.get_secret_value() == "keep-this-secret-1234"
    data = env_path.read_bytes()
    mtime = env_path.stat().st_mtime_ns
    assert sync_env_file(env_path, EXAMPLE) == []
    assert env_path.read_bytes() == data and env_path.stat().st_mtime_ns == mtime
    assert not list(tmp_path.glob("*.tmp"))


def test_sync_refreshing_only_comments_returns_empty_list(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(NL.join(_example_lines()) + NL + "# old note" + NL, encoding="utf-8")
    assert sync_env_file(env_path, EXAMPLE) == []
    assert "# old note" not in env_path.read_text(encoding="utf-8")


def test_sync_does_nothing_without_env_or_template(tmp_path: Path) -> None:
    assert sync_env_file(tmp_path / ".env", EXAMPLE) is None
    assert not (tmp_path / ".env").exists()
    (tmp_path / ".env").write_text("A=1" + NL, encoding="utf-8")
    assert sync_env_file(tmp_path / ".env", tmp_path / "missing.example") is None


def test_envfile_load_renders_full_documented_file(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("# mine" + NL + "LLM_PROVIDER=gemini" + NL, encoding="utf-8")
    env = EnvFile.load(env_path, EXAMPLE)
    assert env.keys() == EnvFile(_example_lines()).keys()
    assert env.get("LLM_PROVIDER") == "gemini"
    assert "# mine" not in env.lines


def test_start_and_serve_refresh_env_and_survive_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import cli

    calls: list[str] = []
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(_env_file=None))

    def fake_start(settings: Settings) -> int:
        calls.append("start")
        return 0

    def fake_serve(settings: Settings, **kw: object) -> int:
        calls.append("serve")
        return 0

    monkeypatch.setattr(cli, "run_start", fake_start)
    monkeypatch.setattr(cli, "run_serve", fake_serve)

    def fake_sync(env: Path, example: Path) -> tuple[bool, list[str]] | None:
        calls.append("sync")
        return True, ["LLM_EFFORT"]

    monkeypatch.setattr(cli, "sync_env_file_detailed", fake_sync)
    p = Scripted([])
    assert cli.main(["start"], prompter=p) == 0
    assert cli.main(["serve"], prompter=p) == 0
    assert calls == ["sync", "start", "sync", "serve"]
    assert any("LLM_EFFORT" in s and "added 1 new option" in s for s in p.said)

    def boom(env: Path, example: Path) -> tuple[bool, list[str]] | None:
        raise PermissionError("secret-path")

    monkeypatch.setattr(cli, "sync_env_file_detailed", boom)
    p2 = Scripted([])
    assert cli.main(["start"], prompter=p2) == 0
    assert calls[-1] == "start"
    assert any("Warning" in s for s in p2.said)


@pytest.mark.parametrize(
    "line",
    [
        "LANGSMITH_API_KEY=                  # tracing is OFF unless this is set",
        "WEB_ALLOWED_DOMAINS=    # optional allow-list",
        "WEB_ALLOWED_DOMAINS=\t# tab before the comment",
        "WEB_ALLOWED_DOMAINS=#not-a-comment",
        "WEB_ALLOWED_DOMAINS=a.edu   # trailing comment",
        'WEB_ALLOWED_DOMAINS="x # y"   # quoted hash',
    ],
)
def test_render_keeps_the_value_dotenv_reads(tmp_path: Path, line: str) -> None:
    """An empty value followed by an inline comment must stay empty (the comment text must never
    become the value: for LANGSMITH_API_KEY that would switch tracing on)."""
    from dotenv import dotenv_values

    from app.setup_flow import render_from_template

    key = line.split("=", 1)[0]
    before = tmp_path / "before.env"
    before.write_text(line + "\n", encoding="utf-8")
    rendered = render_from_template(EXAMPLE.read_text(encoding="utf-8").splitlines(), [line])
    after = tmp_path / "after.env"
    after.write_text("\n".join(rendered) + "\n", encoding="utf-8")
    assert dotenv_values(after)[key] == dotenv_values(before)[key]
    assert EnvFile([line]).get(key) == dotenv_values(before)[key]


# --- local AI (M11) ------------------------------------------------------------------------


def test_menus_list_hosted_providers_first_and_local_last() -> None:
    from app.setup_flow import EMBEDDING_DEFAULTS, LLM_DEFAULTS

    assert list(LLM_DEFAULTS) == ["anthropic", "gemini", "ollama", "openai_compatible"]
    assert list(EMBEDDING_DEFAULTS) == [
        "gemini", "voyage", "openai", "ollama", "openai_compatible", "local",
    ]  # fmt: skip
    assert "fake" not in LLM_DEFAULTS and "fake" not in EMBEDDING_DEFAULTS


def test_choosing_ollama_for_chat_and_embeddings(tmp_path: Path) -> None:
    answers = [
        "", "", "ollama",  # port, LAN, chat provider
        "", "", "",  # Ollama address, chat model, fast model (all defaults)
        "ollama", "", "",  # embedding provider, model, dimension (defaults)
        "",  # Ollama address (embeddings)
        "",  # Tavily key
        *ADMIN_ANSWERS,
    ]  # fmt: skip
    code, p, _, env_path = _run(tmp_path, answers)
    assert code == 0
    env = EnvFile.load(env_path, EXAMPLE)
    assert env.get("LLM_PROVIDER") == "ollama"
    assert env.get("LLM_MODEL") == "qwen2.5:7b-instruct"
    assert env.get("LLM_FAST_MODEL") == "qwen2.5:7b-instruct"
    assert env.get("OLLAMA_BASE_URL") == "http://localhost:11434"
    assert (env.get("EMBEDDING_PROVIDER"), env.get("EMBEDDING_MODEL")) == (
        "ollama",
        "nomic-embed-text",
    )
    assert env.get("EMBEDDING_DIM") == "768"
    shown = p.everything_shown
    assert "for testing" in shown and "AGENT_TIMEOUT_S" in shown and "ollama pull" in shown
    assert "API key" not in "".join(q for q, secret in p.prompts if secret and "Anthropic" in q)
    Settings(_env_file=env_path)  # the written file is valid


def test_openai_compatible_chat_asks_for_address_key_and_model(tmp_path: Path) -> None:
    answers = [
        "", "", "openai_compatible",
        "http://localhost:8001/v1/", "local-key-1",  # address, key (secret)
        "my-model", "",  # chat model; fast model defaults to the chat model
        "", "", "",  # embeddings: gemini defaults
        GOOGLE_KEY, "",
        *ADMIN_ANSWERS,
    ]  # fmt: skip
    code, p, _, env_path = _run(tmp_path, answers)
    assert code == 0
    env = EnvFile.load(env_path, EXAMPLE)
    assert env.get("LOCAL_LLM_BASE_URL") == "http://localhost:8001/v1"
    assert env.get("LOCAL_LLM_API_KEY") == "local-key-1"
    assert (env.get("LLM_MODEL"), env.get("LLM_FAST_MODEL")) == ("my-model", "my-model")
    assert "local-key-1" not in p.everything_shown  # never echoed
    assert Settings(_env_file=env_path).llm_provider == "openai_compatible"


def test_a_bad_local_address_is_asked_again(tmp_path: Path) -> None:
    answers = [
        "", "", "ollama",
        "localhost:11434", "http://ollama:11434", "", "",
        "", "", "", GOOGLE_KEY, "",
        *ADMIN_ANSWERS,
    ]  # fmt: skip
    code, p, _, env_path = _run(tmp_path, answers)
    assert code == 0
    assert "must start with http" in p.everything_shown
    assert EnvFile.load(env_path, EXAMPLE).get("OLLAMA_BASE_URL") == "http://ollama:11434"


def test_local_embeddings_offer_the_install_and_run_it_on_yes(tmp_path: Path) -> None:
    installs: list[int] = []
    fake = FakeDeps()
    deps = fake.build()

    def install() -> bool:
        installs.append(1)
        return True

    deps.install_local_embeddings = install
    answers = [
        "", "", "", "", "", ANTHROPIC_KEY,
        "local", "", "",  # embedding provider, defaults: bge-small, 384
        "y",  # install now?
        "",  # Tavily
        *ADMIN_ANSWERS,
    ]  # fmt: skip
    p = Scripted(answers)
    env_path = tmp_path / ".env"
    code = run_setup(p, deps, env_path=env_path, example_path=EXAMPLE)
    assert code == 0 and installs == [1]
    env = EnvFile.load(env_path, EXAMPLE)
    assert (env.get("EMBEDDING_MODEL"), env.get("EMBEDDING_DIM")) == (
        "BAAI/bge-small-en-v1.5",
        "384",
    )
    assert "1-2 GB" in p.everything_shown and "Installed." in p.everything_shown


def test_local_embeddings_install_declined_shows_the_command(tmp_path: Path) -> None:
    deps = FakeDeps().build()
    deps.install_local_embeddings = lambda: pytest.fail("must not install without a yes")
    answers = ["", "", "", "", "", ANTHROPIC_KEY, "local", "", "", "n", "", *ADMIN_ANSWERS]
    p = Scripted(answers)
    code = run_setup(p, deps, env_path=tmp_path / ".env", example_path=EXAMPLE)
    assert code == 0
    assert "--extra local-embeddings" in p.everything_shown


def test_openai_compatible_embeddings_require_a_model_and_dimension(tmp_path: Path) -> None:
    answers = [
        "", "", "", "", "", ANTHROPIC_KEY,
        "openai_compatible", "bge-large", "abc", "1024",  # model, bad dim, dim
        "http://emb:8002/v1",
        "",
        *ADMIN_ANSWERS,
    ]  # fmt: skip
    code, p, _, env_path = _run(tmp_path, answers)
    assert code == 0
    env = EnvFile.load(env_path, EXAMPLE)
    assert (env.get("EMBEDDING_MODEL"), env.get("EMBEDDING_DIM")) == ("bge-large", "1024")
    assert env.get("LOCAL_EMBEDDING_BASE_URL") == "http://emb:8002/v1"
    assert "values per embedding" in p.everything_shown
