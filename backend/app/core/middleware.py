"""Request-ID middleware: reuse an incoming X-Request-ID or generate one, bind it to logs."""

import re
import uuid
from collections.abc import Sequence

import structlog
from starlette.exceptions import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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
        # A route may set its own stricter policy (document downloads use ``sandbox``).
        if "Content-Security-Policy" not in response.headers:
            response.headers["Content-Security-Policy"] = DOCS_CSP if path == "/docs" else CSP
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if self._hsts:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if not path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response


# Paths that enforce their own, larger body limit: only the document upload route, which
# enforces UPLOAD_MAX_MB itself while streaming (anchored: nothing else under /modules/ matches).
BODY_LIMIT_EXEMPT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/api/v1/modules/[0-9a-f]{32}/documents$"),
)

_TOO_LARGE_MESSAGE = "Request body is too large."


class BodySizeLimitMiddleware:
    """Reject request bodies over ``max_bytes`` with the standard 413 JSON error.

    Checked up front from ``Content-Length`` and again while the body streams (chunked requests
    and lying clients), so a large body is never buffered in full. Paths matching an exempt
    pattern are skipped; they must enforce their own limit.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int,
        exempt: Sequence[re.Pattern[str]] | None = None,
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.exempt = BODY_LIMIT_EXEMPT_PATTERNS if exempt is None else tuple(exempt)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or any(p.match(scope["path"]) for p in self.exempt):
            await self.app(scope, receive, send)
            return
        for name, value in scope["headers"]:
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = 0
                if declared > self.max_bytes:
                    await error_response(413, "payload_too_large", _TOO_LARGE_MESSAGE)(
                        scope, receive, send
                    )
                    return
                break

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # Raised inside the endpoint, so the app's HTTP error handler renders it.
                    raise HTTPException(413, _TOO_LARGE_MESSAGE)
            return message

        await self.app(scope, limited_receive, send)
