"""Programmatic Alembic runner (used by the CLI, start script and tests)."""

from pathlib import Path

from alembic import command
from alembic.config import Config

from app.core.config import Settings

BACKEND_DIR = Path(__file__).resolve().parents[2]


def alembic_config(settings: Settings) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations").replace("%", "%%"))
    cfg.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    cfg.attributes["settings"] = settings
    return cfg


def upgrade_to_head(settings: Settings) -> None:
    """Synchronous; call it outside a running event loop (or via ``asyncio.to_thread``)."""
    command.upgrade(alembic_config(settings), "head")
