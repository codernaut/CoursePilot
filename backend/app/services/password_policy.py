"""Password policy applied wherever a person chooses a password.

Rules: at least ``MIN_PASSWORD_LENGTH`` characters, not blank, not the user's email or its local
part (case-insensitive), and not in the bundled list of very common passwords. Generated
temporary passwords comply by construction. Nothing here imports FastAPI.
"""

import unicodedata
from functools import lru_cache
from pathlib import Path

from app.core.errors import validation_error
from app.services.security import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH

COMMON_PASSWORDS_FILE = Path(__file__).resolve().parents[1] / "resources" / "common_passwords.txt"


@lru_cache
def common_passwords() -> frozenset[str]:
    """Lower-case common passwords (see THIRD_PARTY_NOTICES.md: curated for this project)."""
    text = COMMON_PASSWORDS_FILE.read_text(encoding="utf-8")
    return frozenset(line.strip().lower() for line in text.splitlines() if line.strip())


def _comparable(value: str) -> str:
    """Fold look-alike forms before comparing with the common list and the email: NFKC (full-width
    letters become ASCII), then drop whitespace, control and invisible format characters
    (zero-width spaces), then casefold."""
    folded = unicodedata.normalize("NFKC", value)
    kept = "".join(c for c in folded if unicodedata.category(c)[0] not in ("Z", "C"))
    return kept.casefold()


def validate_password(password: str, *, email: str | None = None) -> None:
    """Raise a 422 ``validation_error`` with a clear message when the password is not allowed."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise validation_error(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise validation_error("Password is too long.")
    if not password.strip():
        raise validation_error("Password cannot be only spaces.")
    lowered = _comparable(password)
    if email:
        address = _comparable(email)
        if lowered in (address, address.split("@", 1)[0]):
            raise validation_error("Password must not be your email address or its first part.")
    if lowered in common_passwords():
        raise validation_error("That password is too common. Choose a less guessable one.")
