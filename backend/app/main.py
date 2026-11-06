"""FastAPI application factory."""

from fastapi import FastAPI

from app.api import health
from app.core.config import Settings, get_settings
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestIdMiddleware

API_PREFIX = "/api/v1"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, json=settings.app_env != "development")

    app = FastAPI(
        title="CoursePilot API",
        version="0.1.0",
        openapi_url=None if settings.is_production else f"{API_PREFIX}/openapi.json",
        # API docs are not public: off in production (and login-gated from M1 on).
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
    )
    app.add_middleware(RequestIdMiddleware)
    register_error_handlers(app)

    app.include_router(health.router, prefix=API_PREFIX)
    return app


app = create_app()
