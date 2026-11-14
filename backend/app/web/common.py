"""Helpers shared by the HTML page routes."""

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from app.core.version import version_label

WEB_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEB_DIR / "static"


def human_size(size: int) -> str:
    """1536 -> '1.5 KB' (binary units)."""
    value = float(size)
    for unit in ("B", "KB", "MB"):
        if value < 1024:
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


@lru_cache(maxsize=64)
def _fingerprint(path: str, mtime_ns: int) -> str:
    return hashlib.sha256((STATIC_DIR / path).read_bytes()).hexdigest()[:12]


def static_url(path: str) -> str:
    """``/static/<path>?v=<content hash>``: the URL changes whenever the file changes, so
    browsers never keep a stale stylesheet or script after an upgrade."""
    file = (STATIC_DIR / path).resolve()
    if not file.is_relative_to(STATIC_DIR.resolve()) or not file.is_file():
        raise ValueError(f"unknown static file: {path}")
    return f"/static/{path}?v={_fingerprint(path, file.stat().st_mtime_ns)}"


def make_templates() -> Jinja2Templates:
    # Jinja2Templates enables autoescape for .html templates. Never mark user, document or
    # model text as safe in a template.
    templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
    templates.env.filters["filesize"] = human_size
    templates.env.globals["static_url"] = static_url
    return templates


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
        "version_label": version_label(),
        "insecure_http": request.app.state.settings.allow_insecure_http
        and not request.app.state.settings.secure_cookies,
    }
    data.update(context or {})
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(request, template, data, status_code=status_code)
