import pytest

from app.core.errors import AppError
from app.services.password_policy import common_passwords, validate_password
from app.services.security import MIN_PASSWORD_LENGTH


def _message(password: str, email: str | None = None) -> str:
    with pytest.raises(AppError) as info:
        validate_password(password, email=email)
    assert info.value.status_code == 422 and info.value.code == "validation_error"
    return info.value.message


def test_good_passwords_pass() -> None:
    validate_password("correct horse battery")
    validate_password("a brand new password", email="someone@example.edu")
    validate_password("   leading spaces are fine 1 ")


def test_too_short() -> None:
    assert "at least 10 characters" in _message("short")
    assert "at least 10 characters" in _message("123456789")


def test_too_long() -> None:
    assert "too long" in _message("x" * 1025)


@pytest.mark.parametrize("password", [" " * 10, " " * 40, "\t" * 12, " \t \n " * 3])
def test_blank_password_is_rejected(password: str) -> None:
    assert "only spaces" in _message(password)


@pytest.mark.parametrize(
    "password",
    ["Jane.Doe@Example.edu", "jane.doe@example.edu", "jane.doe.smith", "JANE.DOE.SMITH"],
)
def test_password_equal_to_email_or_local_part_is_rejected(password: str) -> None:
    email = "Jane.Doe.Smith@example.edu" if "smith" in password.lower() else "jane.doe@example.edu"
    assert "email" in _message(password, email)


def test_email_rule_needs_an_email() -> None:
    validate_password("jane.doe@example.edu")  # not common, no email to compare with


@pytest.mark.parametrize(
    "password",
    ["password123", "PASSWORD123", "Password123", "1234567890", "qwertyuiop", "iloveyou12"],
)
def test_common_passwords_are_rejected_case_insensitively(password: str) -> None:
    assert "too common" in _message(password)


def test_common_password_list_is_bundled_and_sane() -> None:
    passwords = common_passwords()
    assert len(passwords) >= 1000
    assert all(p == p.lower() and p == p.strip() for p in passwords)
    assert all(len(p) >= MIN_PASSWORD_LENGTH for p in passwords)
    assert "password123" in passwords
    assert common_passwords() is passwords  # loaded once


def test_generated_temporary_passwords_comply() -> None:
    from app.services.security import generate_temporary_password

    for _ in range(50):
        validate_password(generate_temporary_password())


ZWSP = chr(0x200B)
FULLWIDTH = "".join(chr(ord(c) + 0xFEE0) for c in "password123")  # NFKC folds these to ASCII


@pytest.mark.parametrize(
    "disguised", [FULLWIDTH, f"pass{ZWSP}word123", "pass word 123", "PASSWORD123"]
)
def test_common_passwords_are_rejected_in_disguised_forms(disguised: str) -> None:
    with pytest.raises(AppError, match="too common"):
        validate_password(disguised)


def test_email_check_ignores_invisible_characters() -> None:
    with pytest.raises(AppError, match="email"):
        validate_password(f"long{ZWSP}.student.name", email="long.student.name@example.edu")
