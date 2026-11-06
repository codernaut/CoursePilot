"""Request-ID middleware: reuse an incoming X-Request-ID or generate one, bind it to logs."""

import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.core.errors import error_response
from app.core.logging import get_logger

log = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
_MAX_REQUEST_ID_LEN = 128


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        valid = 0 < len(incoming) <= _MAX_REQUEST_ID_LEN and incoming.isprintable()
        request_id = incoming if valid else uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


CSP = (
    "default-src 'self'; script-src 'self'; object-src 'none'; frame-ancestors 'none'; "
    "base-uri 'self'; form-action 'self'"
)
# Swagger UI (development only, login required) loads its assets from a CDN and boots with an
# inline script, so /docs gets a relaxed policy. No other route does.
DOCS_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: "
    "https://fastapi.tiangolo.com; object-src 'none'; frame-ancestors 'none'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, *, hsts: bool = False) -> None:
        super().__init__(app)
        self._hsts = hsts

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        try:
            response: Response = await call_next(request)
        except Exception as exc:
            # Handled here (not by Starlette's outermost handler) so that even a 500 carries the
            # security headers and the request id.
            log.exception("unhandled_error", exc_type=type(exc).__name__)
            response = error_response(500, "internal_error", "Internal server error")
        path = request.url.path
        response.headers["Content-Security-Policy"] = DOCS_CSP if path == "/docs" else CSP
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if self._hsts:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if not path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response
