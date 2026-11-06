"""Alembic environment (async SQLite, batch mode, virtual tables excluded from autogenerate)."""

import asyncio
from typing import Any

from alembic import context
from sqlalchemy.engine import Connection

import app.models  # noqa: F401  (registers all tables)
from app.core.config import get_settings
from app.db.base import Base
from app.db.engine import make_engine

config = context.config
target_metadata = Base.metadata

# Virtual tables (and their shadow tables) are created by hand-written migration code.
_VIRTUAL_PREFIXES = ("chunk_vectors", "chunks_fts")


def include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    return not (type_ == "table" and name is not None and name.startswith(_VIRTUAL_PREFIXES))


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        include_object=include_object,
        compare_type=True,
    )


def _run_sync(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = make_engine(_url())
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_sync)
            await connection.commit()
    finally:
        await engine.dispose()


def run_migrations_offline() -> None:
    raise RuntimeError("Offline migrations are not supported (sqlite-vec must be loaded).")


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
