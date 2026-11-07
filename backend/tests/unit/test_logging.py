import io
import json
import logging
from collections.abc import Iterator

import pytest

from app.core.logging import (
    REDACTED,
    _redact_string,
    configure_logging,
    get_logger,
    redact_processor,
)


@pytest.fixture
def stream() -> Iterator[io.StringIO]:
    buffer = io.StringIO()
    yield buffer
    configure_logging("INFO", json=True)  # back to stdout for the other tests


def lines(buffer: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


def redact(**fields: object) -> dict[str, object]:
    return dict(redact_processor(None, "info", {"event": "x", **fields}))


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ant-abcdefgh1234",
        "sk-abcdefghijklmnopqr",
        "AIzaSyA1234567890abcdefghijk",
        "tvly-abcdefghijkl",
        "pa-abcdefghijklmnopqrstuvwxyz0123",
        "Bearer abcdef1234567890",
        "$argon2id$v=19$m=8,t=1,p=1$c29tZXNhbHQ$aGFzaGhhc2hoYXNo",
    ],
)
def test_secret_shapes_are_masked_inside_strings(secret: str) -> None:
    out = redact(error=f"request failed for {secret} twice")
    assert secret not in str(out["error"]) and REDACTED in str(out["error"])


def test_pem_blocks_and_key_value_pairs_are_masked() -> None:
    pem = "-----" + "BEGIN PRIVATE KEY" + "-----\nMIIabc\n-----END PRIVATE KEY-----"
    assert "MIIabc" not in str(redact(detail=pem)["detail"])
    assert "hunter2hunter2" not in str(
        redact(msg="login with password=hunter2hunter2 failed")["msg"]
    )


@pytest.mark.parametrize(
    "name",
    ["password", "new_password", "api_key", "csrf_token", "authorization", "cookie", "prompt"],
)
def test_secret_looking_fields_are_masked_whatever_the_value(name: str) -> None:
    assert redact(**{name: "plain value"})[name] == REDACTED


def test_numbers_and_empty_values_in_secret_looking_fields_are_kept() -> None:
    out = redact(tokens=12, token_count=3, password=None)
    assert out["tokens"] == 12 and out["token_count"] == 3 and out["password"] is None


def test_nested_values_are_masked_and_ordinary_text_is_kept() -> None:
    out = redact(
        details={"headers": {"authorization": "Bearer abcdefgh12345678"}}, note="disk-backed"
    )
    assert "abcdefgh12345678" not in json.dumps(out)
    assert out["note"] == "disk-backed"


def test_structlog_output_is_json_and_redacted(stream: io.StringIO) -> None:
    configure_logging("INFO", json=True, stream=stream)
    get_logger("t").info("provider_failed", api_key="sk-ant-abcdefgh1234", job_id="j1")
    (entry,) = lines(stream)
    assert entry["event"] == "provider_failed" and entry["job_id"] == "j1"
    assert entry["api_key"] == REDACTED and entry["level"] == "info"
    assert "sk-ant" not in stream.getvalue()


def test_standard_library_records_use_the_same_pipeline(stream: io.StringIO) -> None:
    configure_logging("INFO", json=True, stream=stream)
    logging.getLogger("some.library").warning("calling with token=abcd1234efgh5678")
    (entry,) = lines(stream)
    assert entry["level"] == "warning" and "abcd1234efgh5678" not in stream.getvalue()


def test_log_level_is_honoured(stream: io.StringIO) -> None:
    configure_logging("WARNING", json=True, stream=stream)
    get_logger("t").info("quiet")
    get_logger("t").warning("loud")
    logging.getLogger("lib").info("quiet too")
    assert [entry["event"] for entry in lines(stream)] == ["loud"]


def test_exception_text_is_redacted(stream: io.StringIO) -> None:
    configure_logging("INFO", json=True, stream=stream)
    try:
        raise RuntimeError("boom with sk-ant-abcdefgh1234 inside")
    except RuntimeError:
        get_logger("t").exception("failed")
    assert "sk-ant" not in stream.getvalue()


@pytest.mark.parametrize(
    ("text", "leaked"),
    [
        ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA"),
        ("Cookie: __Host-session=abcdef123456", "abcdef123456"),
        ("set-cookie: session=abcdef123456; HttpOnly", "abcdef123456"),
        ("password='hunter2 hunter2'", "hunter2"),
        ('{"password": "hunter2xx"}', "hunter2xx"),
        ("client_secret=abcd1234", "abcd1234"),
        ("db_password=abcd1234 next", "abcd1234"),
        ("access_token: abcdefgh", "abcdefgh"),
    ],
)
def test_more_secret_shapes_are_masked(text: str, leaked: str) -> None:
    assert leaked not in _redact_string(text)


def test_redaction_is_fast_on_adversarial_strings() -> None:
    import time

    for text in (
        "a_" * 100_000,
        "token" * 50_000,
        "token" + " " * 100_000,
        "password=" * 50_000,
        ("-----" + "BEGIN A-----") * 20_000,
        "x-" * 100_000 + "token",
    ):
        started = time.perf_counter()
        _redact_string(text)
        assert time.perf_counter() - started < 2.0


def test_sql_parameters_in_database_errors_are_not_logged(stream: io.StringIO) -> None:
    """SQLAlchemy puts the bound parameters (whole chat messages, document text, e-mails) in the
    text of its errors; a logged traceback must not carry them."""
    from sqlalchemy.exc import IntegrityError

    configure_logging("INFO", json=True, stream=stream)
    try:
        value = "my private" + " question about exam answers"
        raise IntegrityError(
            "INSERT INTO messages (content) VALUES (?)",
            (value,),
            Exception("UNIQUE constraint failed"),
        )
    except IntegrityError:
        get_logger("t").exception("failed")
    assert "private question" not in stream.getvalue()
    assert "UNIQUE constraint failed" in stream.getvalue()
