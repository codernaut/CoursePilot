"""Startup checks shared by the web app and the worker."""

from app.core.config import Settings
from app.core.logging import get_logger

log = get_logger(__name__)


def warn_if_insecure_http(settings: Settings) -> bool:
    """Log a WARNING when ALLOW_INSECURE_HTTP is on in production. Returns whether it logged."""
    if settings.insecure_http_active:
        log.warning(
            "insecure_http_enabled",
            detail="ALLOW_INSECURE_HTTP=true: traffic is not encrypted; use only on a closed, "
            "trusted network",
        )
        return True
    return False
