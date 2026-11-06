"""Setup flow: the "other computers without HTTPS?" question and the password policy prompt."""

from pathlib import Path

import pytest

from app.core.config import Settings
from app.setup_flow import EnvFile, prompt_admin_details
from tests.unit.test_setup_flow import (
    ADMIN_ANSWERS,
    EXAMPLE,
    FRESH_DEFAULTS,
    FakeDeps,
    Scripted,
    _run,
)

LAN_QUESTION = "Will other computers connect to this machine over the network without HTTPS?"


def _lan_answers(url: str, *more: str) -> list[str]:
    # port, LAN=yes, base url [, retries], then the rest of FRESH_DEFAULTS
    return ["", "y", url, *more, *FRESH_DEFAULTS[2:], *ADMIN_ANSWERS]


def test_default_is_this_computer_only(tmp_path: Path) -> None:
    code, p, _, env_path = _run(tmp_path, ["", *FRESH_DEFAULTS[1:], *ADMIN_ANSWERS])
    assert code == 0
    env = EnvFile.load(env_path, EXAMPLE)
    assert env.get("APP_HOST") == "127.0.0.1"
    assert env.get("ALLOW_INSECURE_HTTP") == "false"
    assert env.get("BASE_URL") == "http://localhost:8000"
    asked = [prompt for prompt, secret in p.prompts if not secret and LAN_QUESTION in prompt]
    assert len(asked) == 1 and "[y/N]" in asked[0]
    assert any("this computer only" in s for s in p.said)
    assert not any("clear text" in s for s in p.said)  # no warning when it stays local


def test_lan_yes_sets_host_flag_and_base_url_with_warning(tmp_path: Path) -> None:
    code, p, _, env_path = _run(tmp_path, _lan_answers("http://192.168.1.20:8000/"))
    assert code == 0
    env = EnvFile.load(env_path, EXAMPLE)
    assert env.get("APP_HOST") == "0.0.0.0"  # noqa: S104
    assert env.get("ALLOW_INSECURE_HTTP") == "true"
    assert env.get("BASE_URL") == "http://192.168.1.20:8000"
    assert any("clear text" in s and "closed network" in s for s in p.said)
    settings = Settings(_env_file=env_path)
    assert settings.allow_insecure_http
    assert settings.app_host == "0.0.0.0"  # noqa: S104


@pytest.mark.parametrize(
    "bad",
    [
        "localhost:8000",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "ftp://192.168.1.20",
        "http://",
        "http://192.168.1.20:99999",
        "http://user:pw@192.168.1.20:8000",
        "http://192.168.1.20:8000/app?x=1",
        "not a url",
    ],
)
def test_lan_base_url_is_validated_and_asked_again(tmp_path: Path, bad: str) -> None:
    code, p, _, env_path = _run(tmp_path, _lan_answers(bad, "http://myhost.lan:8000"))
    assert code == 0
    assert EnvFile.load(env_path, EXAMPLE).get("BASE_URL") == "http://myhost.lan:8000"
    assert any("not localhost" in s for s in p.said)


def test_lan_base_url_gives_up_after_repeated_bad_answers(tmp_path: Path) -> None:
    code, p, _, env_path = _run(tmp_path, _lan_answers("nope", *["nope"] * 4))
    assert code == 0
    assert EnvFile.load(env_path, EXAMPLE).get("BASE_URL") == "http://192.168.1.20:8000"
    assert any("Using http://192.168.1.20:8000" in s for s in p.said)


def test_rerun_shows_current_choice_and_keeps_it_on_enter(tmp_path: Path) -> None:
    _run(tmp_path, _lan_answers("http://192.168.1.20:8000"))
    # Enter everywhere, including the LAN question and the base URL.
    code, p, _, env_path = _run(tmp_path, [""] * 12, FakeDeps(admin_exists=True))
    assert code == 0
    env = EnvFile.load(env_path, EXAMPLE)
    assert env.get("ALLOW_INSECURE_HTTP") == "true"
    assert env.get("APP_HOST") == "0.0.0.0"  # noqa: S104
    assert env.get("BASE_URL") == "http://192.168.1.20:8000"  # offered as the default
    assert any("other computers may connect without HTTPS" in s for s in p.said)
    assert any("[Y/n]" in prompt for prompt, _ in p.prompts)


def test_rerun_can_switch_back_to_local_only(tmp_path: Path) -> None:
    _run(tmp_path, _lan_answers("http://192.168.1.20:8000"))
    code, _, _, env_path = _run(tmp_path, ["", "n", *[""] * 9], FakeDeps(admin_exists=True))
    assert code == 0
    env = EnvFile.load(env_path, EXAMPLE)
    assert env.get("APP_HOST") == "127.0.0.1"
    assert env.get("ALLOW_INSECURE_HTTP") == "false"
    assert env.get("BASE_URL") == "http://localhost:8000"  # no stale LAN address left behind


def test_admin_prompt_applies_the_password_policy() -> None:
    p = Scripted(
        [
            "a@example.edu",
            "Ann",
            "password123",  # too common
            "a@example.edu",  # equals the email
            "a good passphrase 1",
            "a good passphrase 1",
        ]
    )
    assert prompt_admin_details(p) == ("a@example.edu", "Ann", "a good passphrase 1")
    shown = " ".join(p.said)
    assert "too common" in shown
    assert "email" in shown


def test_answering_no_keeps_a_host_the_owner_chose(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("APP_HOST=0.0.0.0\n", encoding="utf-8")  # e.g. TLS proxy
    code, _, _, env_path = _run(tmp_path, ["", "n", *[""] * 9], FakeDeps(admin_exists=True))
    assert code == 0
    env = EnvFile.load(env_path, EXAMPLE)
    assert env.get("APP_HOST") == "0.0.0.0"  # noqa: S104
    assert env.get("ALLOW_INSECURE_HTTP") == "false"
