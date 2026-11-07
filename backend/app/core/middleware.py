"""Request-ID middleware: reuse an incoming X-Request-ID or generate one, bind it to logs."""

import re
import time
import uuid
from collections.abc import Sequence

import structlog
from sqlalchemy import inspect as sa_inspect
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


_ID_SEGMENT = re.compile(r"/[0-9a-f]{32}(?=/|$)")
_MAX_ROUTE_CHARS = 200


def _route_label(path: str) -> str:
    """The request path with ids replaced by ``{id}``, so lines group by route. The query string
    is not part of ``scope["path"]`` and is never logged."""
    return _ID_SEGMENT.sub("/{id}", path)[:_MAX_ROUTE_CHARS]


def _user_id(user: object) -> str | None:
    """The user's primary key without touching the database: the ORM instance may be expired or
    detached by the time the response is finished (a rollback expires its attributes)."""
    if user is None:
        return None
    state = sa_inspect(user, raiseerr=False)
    identity = getattr(state, "identity", None)
    if identity:
        return str(identity[0])
    return None


class AccessLogMiddleware:
    """One structured log line per HTTP request: request id, user id (never the email), method,
    route (ids replaced by {id}), status and latency. Query strings, headers, bodies, prompts and
    answers are never logged. Outermost middleware, so it also sees responses produced by the
    others.

    The user id comes from ``request.state.user`` (set by authentication). For streamed responses
    (chat) the latency is the whole stream.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500
        request_id: str | None = None

        async def capture(message: Message) -> None:
            nonlocal status, request_id
            if message["type"] == "http.response.start":
                status = int(message["status"])
                for name, value in message.get("headers", []):
                    if name.lower() == b"x-request-id":
                        request_id = value.decode("latin-1")
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            state = scope.get("state") or {}
            user = state.get("user") if isinstance(state, dict) else getattr(state, "user", None)
            log.info(
                "http_request",
                request_id=request_id,
                user_id=_user_id(user),
                method=scope["method"],
                route=_route_label(scope.get("path", "")),
                status=status,
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
            )
