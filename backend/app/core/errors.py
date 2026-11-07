"""One JSON error shape for the whole API: {"error": {"code", "message", "details"}}."""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger
from app.models.enums import Role

log = get_logger(__name__)

PROVIDER_NOT_CONFIGURED_MESSAGE = "AI provider not configured. Ask an administrator."


class AppError(Exception):
    """Raise from services; converted to the standard error response."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
        admin_message: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        # A longer message (with the setup hint) that only administrators are shown.
        self.admin_message = admin_message

    def message_for(self, is_admin: bool) -> str:
        return self.admin_message if is_admin and self.admin_message else self.message


def provider_not_configured(hint: str) -> AppError:
    """503 ``provider_not_configured``: everyone gets the short message, administrators also the
    ``hint`` (which setting is missing)."""
    return AppError(
        "provider_not_configured",
        PROVIDER_NOT_CONFIGURED_MESSAGE,
        status_code=503,
        admin_message=f"AI provider not configured: {hint}",
    )


def error_response(
    status_code: int, code: str, message: str, details: dict[str, Any] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": details or {}}},
    )


def unauthorized(message: str = "Authentication required.") -> AppError:
    return AppError("unauthorized", message, status_code=401)


def forbidden(message: str = "You do not have permission to do this.") -> AppError:
    return AppError("forbidden", message, status_code=403)


def not_found(message: str = "Not found.") -> AppError:
    return AppError("not_found", message, status_code=404)


def conflict(message: str, details: dict[str, Any] | None = None) -> AppError:
    return AppError("conflict", message, status_code=409, details=details)


def validation_error(message: str, details: dict[str, Any] | None = None) -> AppError:
    return AppError("validation_error", message, status_code=422, details=details)


_HTTP_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    429: "rate_limited",
}


def _page_redirect(request: Request, exc: AppError) -> Response | None:
    """Browsers asking for a page (not the JSON API) are sent to the right form instead."""
    if request.url.path.startswith("/api/"):
        return None
    target = None
    if exc.status_code == 401:
        target = "/login"
    elif exc.code == "password_change_required":
        target = "/change-password"
    if target is None:
        return None
    if request.headers.get("hx-request"):
        return Response(status_code=200, headers={"HX-Redirect": target})
    return RedirectResponse(target, status_code=303)


async def _app_error_handler(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, AppError):  # pragma: no cover - registration guarantees type
        raise exc
    redirect = _page_redirect(request, exc)
    if redirect is not None:
        return redirect
    is_admin = False
    if exc.admin_message:  # only then does the role matter (and the user may be detached)
        user = getattr(request.state, "user", None)
        try:
            is_admin = getattr(user, "role", None) == Role.ADMIN
        except Exception:  # e.g. a detached ORM instance: fall back to the short message
            is_admin = False
    return error_response(exc.status_code, exc.code, exc.message_for(is_admin), exc.details)


async def _http_error_handler(_: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, StarletteHTTPException):  # pragma: no cover
        raise exc
    code = _HTTP_CODES.get(exc.status_code, "http_error")
    return error_response(exc.status_code, code, str(exc.detail))


async def _validation_error_handler(_: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):  # pragma: no cover
        raise exc
    return error_response(
        422, "validation_error", "Request validation failed", {"errors": exc.errors()}
    )


async def _unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled_error", exc_type=type(exc).__name__)
    return error_response(500, "internal_error", "Internal server error")


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)
