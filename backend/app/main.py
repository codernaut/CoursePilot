"""FastAPI application factory."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.agent.runtime import AgentRuntime
from app.api.deps import CurrentUser
from app.api.router import api_router
from app.core.config import Settings, get_settings
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging
from app.core.middleware import (
    BodySizeLimitMiddleware,
    RequestIdMiddleware,
    SecurityHeadersMiddleware,
)
from app.core.startup import warn_if_insecure_http
from app.core.version import get_commit
from app.db.session import Database
from app.ingestion.embedder import Embedder
from app.llm.factory import LLMFactory
from app.retrieval.query_embedding import QueryEmbedder
from app.services.auth import LoginThrottle
from app.services.chat import make_turn_registry
from app.storage import get_storage
from app.web.common import STATIC_DIR, make_templates
from app.web.routes import (
    admin_pages,
    auth_pages,
    chat_pages,
    materials_pages,
    search_pages,
    structure_pages,
)
from app.websearch.factory import WebSearchFactory, build_web_search

API_PREFIX = "/api/v1"


def create_app(
    settings: Settings | None = None,
    *,
    embedder_factory: Callable[[], Embedder] | None = None,
    llm_factory: LLMFactory | None = None,
    web_search_factory: WebSearchFactory | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, json=settings.app_env != "development")
    warn_if_insecure_http(settings)
    get_commit()  # resolve once at startup; failures yield "unknown"
    database = Database(settings.database_url)

    query_embedder = QueryEmbedder(settings, factory=embedder_factory)
    web_search = (web_search_factory or build_web_search)(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await query_embedder.aclose()
        if web_search is not None:
            await web_search.aclose()
        await database.dispose()

    app = FastAPI(
        title="CoursePilot API",
        version=__version__,
        # The built-in docs routes are public, so they are replaced by login-gated ones below.
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.login_throttle = LoginThrottle(settings.login_rate_limit_per_min)
    app.state.password_throttle = LoginThrottle(settings.login_rate_limit_per_min)
    app.state.templates = make_templates()
    app.state.storage = get_storage(settings)
    app.state.query_embedder = query_embedder
    app.state.agent_runtime = AgentRuntime(settings, llm_factory)
    app.state.web_search = web_search
    app.state.chat_turns = make_turn_registry(settings)

    # Added first = innermost, so its 413 responses still get the security headers and request id.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.secure_cookies)
    app.add_middleware(RequestIdMiddleware)
    register_error_handlers(app)

    app.include_router(api_router, prefix=API_PREFIX)
    app.include_router(auth_pages.router)
    app.include_router(admin_pages.router)
    app.include_router(structure_pages.router)
    app.include_router(materials_pages.router)
    app.include_router(search_pages.router)
    app.include_router(chat_pages.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    if not settings.is_production:

        @app.get("/docs", include_in_schema=False)
        async def docs(_: CurrentUser) -> HTMLResponse:
            return get_swagger_ui_html(
                openapi_url=f"{API_PREFIX}/openapi.json", title="CoursePilot API"
            )

        @app.get(f"{API_PREFIX}/openapi.json", include_in_schema=False)
        async def openapi_json(_: CurrentUser) -> JSONResponse:
            return JSONResponse(app.openapi())

    return app


app = create_app()
