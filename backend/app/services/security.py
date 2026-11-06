"""Security primitives: password hashing, token generation, login-CSRF tokens, origin checks."""

import hashlib
import secrets
from functools import lru_cache
from urllib.parse import urlsplit

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

from app.core.config import Settings

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 1024
LOGIN_TOKEN_MAX_AGE_S = 30 * 60


@lru_cache
def _hasher() -> PasswordHash:
    return PasswordHash((Argon2Hasher(),))


def hash_password(password: str) -> str:
    return _hasher().hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher().verify(password, password_hash)
    except Exception:
        return False


@lru_cache
def _dummy_hash() -> str:
    return hash_password(secrets.token_urlsafe(16))


def verify_against_dummy(password: str) -> None:
    """Burn the same time as a real verification so unknown users are not detectable by timing."""
    verify_password(password, _dummy_hash())


def generate_temporary_password() -> str:
    return secrets.token_urlsafe(12)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode(), b.encode())


def _login_serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key.get_secret_value(), salt="login-csrf")


def make_login_token(settings: Settings) -> str:
    return _login_serializer(settings).dumps(secrets.token_hex(8))


def verify_login_token(settings: Settings, token: str | None) -> bool:
    if not token:
        return False
    try:
        _login_serializer(settings).loads(token, max_age=LOGIN_TOKEN_MAX_AGE_S)
    except (BadSignature, SignatureExpired):
        return False
    return True


def _origin_of(url: str) -> str | None:
    try:
        parts = urlsplit(url)
        if not parts.scheme or not parts.hostname:
            return None
        if parts.username is not None or parts.password is not None:
            return None  # a real Origin header never carries userinfo
        default_port = {"http": 80, "https": 443}.get(parts.scheme)
        port = parts.port or default_port
    except ValueError:  # malformed port or IPv6 literal
        return None
    return f"{parts.scheme}://{parts.hostname.lower()}:{port}"


def allowed_origins(settings: Settings) -> set[str]:
    origins = {o for o in (_origin_of(settings.base_url),) if o}
    if not settings.is_production:
        for host in ("localhost", "127.0.0.1"):
            origin = _origin_of(f"http://{host}:{settings.app_port}")
            if origin:
                origins.add(origin)
    return origins


def origin_allowed(settings: Settings, origin: str | None, referer: str | None) -> bool:
    """Origin (else Referer) must match BASE_URL. Neither header: allowed (non-browser client;
    a valid CSRF token is still required)."""
    candidate = origin if origin else referer
    if not candidate:
        return True
    if candidate == "null":
        return False
    found = _origin_of(candidate)
    return found is not None and found in allowed_origins(settings)
