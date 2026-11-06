"""Helpers shared by the HTML page routes."""

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

WEB_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEB_DIR / "static"


def make_templates() -> Jinja2Templates:
    # Jinja2Templates enables autoescape for .html templates. Never mark user, document or
    # model text as safe in a template.
    return Jinja2Templates(directory=str(WEB_DIR / "templates"))


def render(
    request: Request,
    template: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
) -> Response:
    data: dict[str, Any] = {
        "user": getattr(request.state, "user", None),
        "csrf_token": getattr(request.state, "csrf_token", None),
    }
    data.update(context or {})
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(request, template, data, status_code=status_code)
