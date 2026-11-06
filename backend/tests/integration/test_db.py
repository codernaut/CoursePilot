import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlite_vec
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from fastapi import FastAPI
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.db.base import Base, utcnow
from app.db.migrate import alembic_config, upgrade_to_head
from app.models import Block, Module, User, UserSession, Year
from app.models.enums import Role
from app.services import structure
from tests.conftest import make_settings

EXPECTED_TABLES = {
    "app_meta",
    "audit_log",
    "blocks",
    "chunk_vectors",
    "chunks",
    "chunks_fts",
    "conversations",
    "documents",
    "jobs",
    "messages",
    "modules",
    "sessions",
    "users",
    "years",
}


def _run_in_thread(fn: Callable[..., Any], *args: Any) -> Any:
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(fn, *args).result()


def _sqlite(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    return conn


@pytest.fixture
def fresh(tmp_path: Path) -> Settings:
    """Settings pointing at a database that has NOT been migrated yet."""
    return make_settings(tmp_path, embedding_dim=16)


def test_migration_creates_everything_on_fresh_db(fresh: Settings, tmp_path: Path) -> None:
    _run_in_thread(upgrade_to_head, fresh)
    conn = _sqlite(tmp_path / "test.db")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables >= EXPECTED_TABLES
    assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("0001",)
    meta = dict(conn.execute("SELECT key, value FROM app_meta"))
    assert meta == {
        "embedding_provider": fresh.embedding_provider,
        "embedding_model": fresh.embedding_model,
        "embedding_dim": "16",
    }


def test_virtual_tables_exist_with_configured_dimension(fresh: Settings, tmp_path: Path) -> None:
    _run_in_thread(upgrade_to_head, fresh)
    conn = _sqlite(tmp_path / "test.db")
    ddl = dict(
        conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE name IN ('chunk_vectors', 'chunks_fts')"
        )
    )
    assert "vec0" in ddl["chunk_vectors"]
    assert "float[16]" in ddl["chunk_vectors"]
    assert "distance_metric=cosine" in ddl["chunk_vectors"]
    assert "fts5" in ddl["chunks_fts"].lower()
    assert "porter unicode61" in ddl["chunks_fts"]


def test_vector_and_fulltext_round_trip(fresh: Settings, tmp_path: Path) -> None:
    _run_in_thread(upgrade_to_head, fresh)
    conn = _sqlite(tmp_path / "test.db")
    near = [1.0] + [0.0] * 15
    far = [0.0, 1.0] + [0.0] * 14
    conn.execute(
        "INSERT INTO chunk_vectors(rowid, embedding) VALUES (?, ?)",
        (1, sqlite_vec.serialize_float32(near)),
    )
    conn.execute(
        "INSERT INTO chunk_vectors(rowid, embedding) VALUES (?, ?)",
        (2, sqlite_vec.serialize_float32(far)),
    )
    rows = conn.execute(
        "SELECT rowid, distance FROM chunk_vectors WHERE embedding MATCH ? AND k = 2 "
        "ORDER BY distance",
        (sqlite_vec.serialize_float32(near),),
    ).fetchall()
    assert [r[0] for r in rows] == [1, 2]
    assert rows[0][1] == pytest.approx(0.0, abs=1e-6)
    conn.execute("INSERT INTO chunks_fts(rowid, embed_text) VALUES (1, 'Running quickly')")
    hit = conn.execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?", ('"run"',)
    ).fetchall()
    assert hit == [(1,)]  # porter stemming


def test_models_and_migration_agree(fresh: Settings, tmp_path: Path) -> None:
    """No pending autogenerate differences (virtual tables are excluded by name)."""
    _run_in_thread(upgrade_to_head, fresh)
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")

    @event.listens_for(engine, "connect")
    def _load_vec(dbapi_connection, _record):  # type: ignore[no-untyped-def]
        dbapi_connection.enable_load_extension(True)
        sqlite_vec.load(dbapi_connection)

    def include(obj, name, type_, reflected, compare_to):  # type: ignore[no-untyped-def]
        return not (type_ == "table" and name and name.startswith(("chunk_vectors", "chunks_fts")))

    with engine.connect() as connection:
        ctx = MigrationContext.configure(
            connection,
            opts={"include_object": include, "render_as_batch": True},
        )
        diff = compare_metadata(ctx, Base.metadata)
    engine.dispose()
    assert diff == []


def test_downgrade_removes_everything(fresh: Settings, tmp_path: Path) -> None:
    _run_in_thread(upgrade_to_head, fresh)
    _run_in_thread(command.downgrade, alembic_config(fresh), "base")
    conn = _sqlite(tmp_path / "test.db")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not (EXPECTED_TABLES & tables)


def test_upgrade_is_idempotent(fresh: Settings) -> None:
    _run_in_thread(upgrade_to_head, fresh)
    _run_in_thread(upgrade_to_head, fresh)


async def test_connection_pragmas_and_vec_loaded(app: FastAPI) -> None:
    async with app.state.database.session() as db:
        assert (await db.execute(text("PRAGMA journal_mode"))).scalar() == "wal"
        assert (await db.execute(text("PRAGMA foreign_keys"))).scalar() == 1
        assert (await db.execute(text("PRAGMA busy_timeout"))).scalar() == 10_000
        assert (await db.execute(text("PRAGMA synchronous"))).scalar() == 1  # NORMAL
        assert (await db.execute(text("SELECT vec_version()"))).scalar()


async def test_foreign_keys_are_enforced(app: FastAPI) -> None:
    async with app.state.database.session() as db:
        db.add(
            UserSession(
                user_id="0" * 32,
                token_hash="a" * 64,
                csrf_token="c",
                expires_at=utcnow(),
                last_seen_at=utcnow(),
            )
        )
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_write_sessions_use_begin_immediate_and_read_sessions_do_not(
    app: FastAPI, settings: Settings
) -> None:
    database = app.state.database
    path = settings.database_url.removeprefix("sqlite+aiosqlite:///")
    async with database.session() as writer:
        await writer.execute(text("SELECT 1"))  # first statement opens BEGIN IMMEDIATE
        other = sqlite3.connect(path, timeout=0.1)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")  # the write lock is already held
        finally:
            other.close()
        async with database.read_session() as reader:  # readers are not blocked (WAL)
            assert (await reader.execute(text("SELECT count(*) FROM users"))).scalar() == 0


async def test_timestamps_round_trip_as_aware_utc(app: FastAPI) -> None:
    async with app.state.database.session() as db:
        user = User(
            email="t@example.edu",
            display_name="T",
            role=Role.STUDENT,
            password_hash="x",
        )
        db.add(user)
        await db.commit()
    async with app.state.database.read_session() as db:
        row = (await db.execute(select(User))).scalar_one()
    assert row.created_at.tzinfo is UTC
    assert abs((datetime.now(UTC) - row.created_at).total_seconds()) < 60
    assert row.must_change_password is True  # new accounts must change their password
    assert len(row.id) == 32


async def test_naive_datetimes_are_rejected(app: FastAPI) -> None:
    from sqlalchemy.exc import StatementError

    async with app.state.database.session() as db:
        db.add(
            UserSession(
                user_id="0" * 32,
                token_hash="b" * 64,
                csrf_token="c",
                expires_at=datetime(2030, 1, 1),
                last_seen_at=utcnow(),
            )
        )
        with pytest.raises((StatementError, ValueError)):
            await db.commit()


async def test_seed_structure_counts(app: FastAPI) -> None:
    async with app.state.database.session() as db:
        result = await structure.seed_structure(db, years=3, blocks_per_year=3, modules_per_block=3)
    assert (result.years, result.blocks, result.modules) == (3, 9, 27)
    async with app.state.database.read_session() as db:
        assert len((await db.execute(select(Module))).scalars().all()) == 27
        years = (await db.execute(select(Year).order_by(Year.number))).scalars().all()
        assert [y.title for y in years] == ["Year 1", "Year 2", "Year 3"]


async def test_seed_structure_is_idempotent(app: FastAPI) -> None:
    async with app.state.database.session() as db:
        await structure.seed_structure(db, years=3, blocks_per_year=3, modules_per_block=3)
    async with app.state.database.session() as db:
        again = await structure.seed_structure(db, years=3, blocks_per_year=3, modules_per_block=3)
    assert again.total == 0
    async with app.state.database.read_session() as db:
        assert len((await db.execute(select(Module))).scalars().all()) == 27


async def test_seed_structure_never_renames_or_deletes(app: FastAPI) -> None:
    async with app.state.database.session() as db:
        await structure.seed_structure(db, years=3, blocks_per_year=3, modules_per_block=3)
    async with app.state.database.session() as db:
        year = (await db.execute(select(Year).where(Year.number == 1))).scalar_one()
        year.title = "Foundations"
        module = (await db.execute(select(Module).order_by(Module.number))).scalars().first()
        assert module is not None
        module.title = "Custom"
        module.description = "kept"
        module_id = module.id
        await db.commit()
    async with app.state.database.session() as db:
        # Growing adds only the missing rows ...
        grown = await structure.seed_structure(db, years=4, blocks_per_year=3, modules_per_block=4)
        assert grown.years == 1
        assert (grown.blocks, grown.modules) == (3, 21)  # new year: 3x4; old blocks: +1 each
    async with app.state.database.session() as db:
        # ... and shrinking deletes nothing.
        shrunk = await structure.seed_structure(db, years=1, blocks_per_year=1, modules_per_block=1)
        assert shrunk.total == 0
    async with app.state.database.read_session() as db:
        assert (
            await db.execute(select(Year).where(Year.number == 1))
        ).scalar_one().title == "Foundations"
        module = await db.get(Module, module_id)
        assert module is not None
        assert (module.title, module.description) == ("Custom", "kept")
        assert len((await db.execute(select(Year))).scalars().all()) == 4
        assert len((await db.execute(select(Block))).scalars().all()) == 12
