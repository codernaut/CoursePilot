import pytest

from app.core.config import Settings
from app.services import security
from app.services.security import (
    constant_time_equals,
    generate_temporary_password,
    hash_password,
    hash_token,
    make_login_token,
    new_token,
    origin_allowed,
    verify_login_token,
    verify_password,
)

SETTINGS = Settings(_env_file=None, base_url="https://example.edu", secret_key="s" * 40)


def test_password_hash_is_argon2_and_verifies() -> None:
    hashed = hash_password("a long enough password")
    assert hashed.startswith("$argon2")
    assert "a long enough password" not in hashed
    assert verify_password("a long enough password", hashed)
    assert not verify_password("another password!", hashed)


def test_password_hashes_are_salted() -> None:
    assert hash_password("same password 1") != hash_password("same password 1")


def test_verify_password_rejects_garbage_hashes() -> None:
    assert not verify_password("x", "not-a-hash")
    assert not verify_password("x", "")


def test_dummy_verification_does_not_raise() -> None:
    security.verify_against_dummy("whatever")


def test_tokens_are_unique_and_hashed() -> None:
    a, b = new_token(), new_token()
    assert a != b and len(a) >= 43
    assert hash_token(a) == hash_token(a)
    assert hash_token(a) != a and len(hash_token(a)) == 64


def test_temporary_passwords_meet_the_minimum_length() -> None:
    values = {generate_temporary_password() for _ in range(20)}
    assert len(values) == 20
    assert all(len(v) >= security.MIN_PASSWORD_LENGTH for v in values)


def test_constant_time_equals() -> None:
    assert constant_time_equals("abc", "abc")
    assert not constant_time_equals("abc", "abd")
    assert not constant_time_equals("abc", "abcd")


def test_login_token_roundtrip_and_tamper_detection() -> None:
    token = make_login_token(SETTINGS)
    assert verify_login_token(SETTINGS, token)
    assert not verify_login_token(SETTINGS, token + "x")
    assert not verify_login_token(SETTINGS, None)
    assert not verify_login_token(SETTINGS, "")
    other = Settings(_env_file=None, secret_key="o" * 40)
    assert not verify_login_token(other, token)  # signed with a different SECRET_KEY


def test_login_token_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    token = make_login_token(SETTINGS)
    monkeypatch.setattr(security, "LOGIN_TOKEN_MAX_AGE_S", -1)
    assert not verify_login_token(SETTINGS, token)


@pytest.mark.parametrize(
    ("origin", "referer", "allowed"),
    [
        ("https://example.edu", None, True),
        ("https://example.edu:443", None, True),
        ("HTTPS://EXAMPLE.EDU", None, True),
        ("https://evil.example", None, False),
        ("http://example.edu", None, False),  # scheme downgrade
        ("https://example.edu.evil.example", None, False),
        ("https://example.edu:8443", None, False),
        ("null", None, False),
        (None, "https://example.edu/page?x=1", True),
        (None, "https://evil.example/page", False),
        (None, None, True),  # non-browser client; the CSRF token is still required
        ("", "https://evil.example/", False),  # empty Origin falls back to Referer
        ("not a url", None, False),
        ("https://evil.example", "https://example.edu/", False),  # Origin wins over Referer
    ],
)
def test_origin_allowed(origin: str | None, referer: str | None, allowed: bool) -> None:
    assert origin_allowed(SETTINGS, origin, referer) is allowed


def test_dev_origins_include_loopback_variants() -> None:
    dev = Settings(_env_file=None, base_url="http://localhost:8000", app_port=8000)
    assert origin_allowed(dev, "http://127.0.0.1:8000", None)
    assert origin_allowed(dev, "http://localhost:8000", None)
    assert not origin_allowed(dev, "http://127.0.0.1:9999", None)
    prod = Settings(
        _env_file=None,
        app_env="production",
        base_url="https://example.edu",
        secret_key="s" * 40,
    )
    assert not origin_allowed(prod, "http://localhost:8000", None)
