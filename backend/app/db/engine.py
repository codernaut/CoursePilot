"""Async SQLite engine with sqlite-vec, WAL and the per-connection pragmas (CLAUDE.md section 3).

Every connection loads sqlite-vec and sets journal_mode=WAL, busy_timeout, foreign_keys=ON and
synchronous=NORMAL. Write transactions start with BEGIN IMMEDIATE; read-only sessions use a plain
deferred BEGIN so readers never queue behind the single writer.
"""

import sqlite3
from pathlib import Path
from typing import Any

import sqlite_vec
from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

BUSY_TIMEOUT_MS = 10_000
MIN_SQLITE_VERSION = (3, 35, 0)
READ_ONLY_OPTION = "sqlite_read_only"
_SQLITE_PREFIX = "sqlite+aiosqlite:///"


class SQLiteCapabilityError(RuntimeError):
    """SQLite on this machine cannot run CoursePilot (readable message for setup / health)."""


def sqlite_path(database_url: str) -> Path | None:
    """The file path of a sqlite+aiosqlite URL, or None for in-memory databases."""
    if not database_url.startswith(_SQLITE_PREFIX):
        raise ValueError("DATABASE_URL must start with sqlite+aiosqlite:///")
    raw = database_url[len(_SQLITE_PREFIX) :].split("?", 1)[0]
    if not raw or raw.startswith(":memory:"):
        return None
    return Path(raw)


async def _configure_connection(driver_connection: Any) -> None:
    await driver_connection.enable_load_extension(True)
    try:
        await driver_connection.load_extension(sqlite_vec.loadable_path())
    finally:
        await driver_connection.enable_load_extension(False)
    await driver_connection.execute("PRAGMA journal_mode=WAL")
    await driver_connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    await driver_connection.execute("PRAGMA foreign_keys=ON")
    await driver_connection.execute("PRAGMA synchronous=NORMAL")


def make_engine(database_url: str) -> AsyncEngine:
    path = sqlite_path(database_url)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(database_url)

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        # We control transactions ourselves (see _on_begin), so the driver must not auto-begin.
        dbapi_connection.isolation_level = None
        dbapi_connection.run_async(_configure_connection)

    @event.listens_for(engine.sync_engine, "begin")
    def _on_begin(conn: Connection) -> None:
        if conn.get_execution_options().get(READ_ONLY_OPTION):
            conn.exec_driver_sql("BEGIN")
        else:
            conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def check_sqlite_capabilities() -> list[str]:
    """Return a list of problems (empty when SQLite, FTS5 and sqlite-vec are all usable)."""
    problems: list[str] = []
    if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
        problems.append(
            f"SQLite {sqlite3.sqlite_version} is too old; CoursePilot needs >= "
            + ".".join(map(str, MIN_SQLITE_VERSION))
        )
    conn = sqlite3.connect(":memory:")
    try:
        try:
            conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        except sqlite3.Error:
            problems.append("This SQLite build has no FTS5 support.")
        if not hasattr(conn, "enable_load_extension"):
            problems.append(
                "This Python build cannot load SQLite extensions (enable_load_extension is "
                "missing); use the python.org build or another Python with extension support."
            )
        else:
            try:
                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
                conn.execute("SELECT vec_version()").fetchone()
            except (sqlite3.Error, OSError) as exc:
                problems.append(f"sqlite-vec could not be loaded: {exc}")
    finally:
        conn.close()
    return problems
