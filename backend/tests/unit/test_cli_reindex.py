"""``reindex-all`` on the command line, and the embedding questions of ``setup``."""

import sqlite3
from pathlib import Path

import pytest

from app import cli
from app.core.config import Settings
from app.setup_flow import EnvFile
from tests.conftest import make_settings
from tests.unit.test_setup_flow import (
    ADMIN_ANSWERS,
    EXAMPLE,
    FRESH_DEFAULTS,
    GOOGLE_KEY,
    Scripted,
    _run,
)


def _db(settings: Settings) -> sqlite3.Connection:
    return sqlite3.connect(settings.database_url.removeprefix("sqlite+aiosqlite:///"))


def _fake_settings(tmp_path: Path, **kw: object) -> Settings:
    return make_settings(
        tmp_path, embedding_provider="fake", embedding_model="fake-hash-v2", embedding_dim=8, **kw
    )


def _meta(settings: Settings) -> dict[str, str]:
    conn = _db(settings)
    try:
        return dict(conn.execute("SELECT key, value FROM app_meta").fetchall())
    finally:
        conn.close()


def _seed_document(settings: Settings) -> None:
    ts = "'2026-01-01 00:00:00'"
    statements = [
        f"INSERT INTO years(id, number, title, created_at, updated_at) "
        f"VALUES ('y', 1, 'Y', {ts}, {ts})",
        f"INSERT INTO blocks(id, year_id, number, title, created_at, updated_at) "
        f"VALUES ('b', 'y', 1, 'B', {ts}, {ts})",
        f"INSERT INTO modules(id, block_id, number, title, description, web_search_enabled, "
        f"created_at, updated_at) VALUES ('m', 'b', 1, 'M', '', 1, {ts}, {ts})",
        f"INSERT INTO users(id, email, password_hash, display_name, role, is_active, "
        f"must_change_password, created_at, updated_at) "
        f"VALUES ('u', 'f@example.edu', 'x', 'F', 'faculty', 1, 0, {ts}, {ts})",
        f"INSERT INTO documents(id, module_id, uploaded_by, title, original_filename, mime_type, "
        f"size_bytes, storage_key, content_sha256, status, created_at, updated_at) "
        f"VALUES ('d', 'm', 'u', 'T', 'a.md', 'text/markdown', 1, 'k', 's', 'ready', {ts}, {ts})",
    ]
    conn = _db(settings)
    try:
        for statement in statements:
            conn.execute(statement)
        conn.commit()
    finally:
        conn.close()


def test_reindex_all_asks_for_confirmation_and_cancels_cleanly(
    settings: Settings, tmp_path: Path
) -> None:
    fake = _fake_settings(tmp_path)
    p = Scripted(["n"])
    assert cli.run_reindex_all(fake, p) == 1
    assert _meta(fake)["embedding_provider"] == "gemini"  # nothing changed
    assert any("Cancelled" in s for s in p.said)
    assert p.prompts and "Continue?" in p.prompts[0][0]


def test_reindex_all_recreates_vectors_updates_meta_and_queues_jobs(
    settings: Settings, tmp_path: Path
) -> None:
    fake = _fake_settings(tmp_path)
    _seed_document(fake)
    p = Scripted([])
    assert cli.run_reindex_all(fake, p, yes=True) == 0  # --yes: no prompt
    assert _meta(fake) == {
        "embedding_provider": "fake",
        "embedding_model": "fake-hash-v2",
        "embedding_dim": "8",
    }
    conn = _db(fake)
    try:
        conn.enable_load_extension(True)
        import sqlite_vec

        sqlite_vec.load(conn)
        conn.execute(
            "INSERT INTO chunk_vectors(rowid, embedding) VALUES (1, ?)",
            (sqlite_vec.serialize_float32([0.5] * 8),),  # the new table takes 8 dimensions
        )
        status = conn.execute("SELECT status FROM documents").fetchone()[0]
        jobs = conn.execute("SELECT type, status, payload FROM jobs").fetchall()
    finally:
        conn.close()
    assert status == "uploaded"
    assert jobs == [("reindex_document", "queued", '{"document_id": "d"}')]
    assert any("1 document(s) queued" in s for s in p.said)


def test_reindex_all_refuses_when_the_provider_has_no_key(settings: Settings) -> None:
    p = Scripted([])  # default settings: gemini without GOOGLE_API_KEY
    assert cli.run_reindex_all(settings, p, yes=True) == 1
    assert any("not configured" in s and "GOOGLE_API_KEY" in s for s in p.said)
    assert _meta(settings)["embedding_dim"] == "768"


def test_reindex_all_is_a_cli_command() -> None:
    args = cli.build_parser().parse_args(["reindex-all", "--yes"])
    assert args.command == "reindex-all" and args.yes is True
    assert cli.build_parser().parse_args(["reindex-all"]).yes is False


def test_setup_always_mentions_reindex_all(tmp_path: Path) -> None:
    _, p, _, _ = _run(tmp_path, ["", *FRESH_DEFAULTS[1:], *ADMIN_ANSWERS])
    assert any("reindex-all" in s for s in p.said)
    assert not any("changed in this run" in s for s in p.said)


def test_setup_gemini_default_shares_the_google_key_with_chat(tmp_path: Path) -> None:
    answers = ["", "", "gemini", "", "", GOOGLE_KEY, "", "", "", "", *ADMIN_ANSWERS]
    code, p, _, env_path = _run(tmp_path, answers)
    assert code == 0
    env = EnvFile.load(env_path, EXAMPLE)
    assert env.get("GOOGLE_API_KEY") == GOOGLE_KEY
    assert (env.get("EMBEDDING_PROVIDER"), env.get("EMBEDDING_DIM")) == ("gemini", "768")
    keys_asked = [prompt for prompt, secret in p.prompts if secret and "API key" in prompt]
    assert len(keys_asked) == 2  # Google (once, for chat AND embeddings) + Tavily


@pytest.mark.parametrize(
    ("provider", "model", "dim", "var"),
    [
        ("voyage", "voyage-3.5", "1024", "VOYAGE_API_KEY"),
        ("openai", "text-embedding-3-small", "1536", "OPENAI_API_KEY"),
    ],
)
def test_setup_voyage_and_openai_defaults_and_keys(
    tmp_path: Path, provider: str, model: str, dim: str, var: str
) -> None:
    answers = ["", "", "", "", "", "", provider, "", "", "secret-key-123456", "", *ADMIN_ANSWERS]
    _, p, _, env_path = _run(tmp_path, answers)
    env = EnvFile.load(env_path, EXAMPLE)
    assert (env.get("EMBEDDING_MODEL"), env.get("EMBEDDING_DIM")) == (model, dim)
    assert env.get(var) == "secret-key-123456"
    assert any("changed in this run" in s for s in p.said)  # switched away from the default
    assert "secret-key-123456" not in p.everything_shown


def test_reindex_all_refuses_while_a_worker_is_running_a_job(
    settings: Settings, tmp_path: Path
) -> None:
    """A live worker (fresh heartbeat on a running job) would embed with the old vectors table."""
    fake = _fake_settings(tmp_path)
    _seed_document(fake)
    now = "strftime('%Y-%m-%d %H:%M:%f','now')"
    conn = _db(fake)
    try:
        conn.execute(
            "INSERT INTO jobs(id, type, payload, status, attempts, max_attempts, run_after, "
            f"locked_at, locked_by, created_at, updated_at) VALUES ('j', 'ingest_document', "
            f"'{{\"document_id\": \"d\"}}', 'running', 1, 3, {now}, {now}, 'w', {now}, {now})"
        )
        conn.commit()
    finally:
        conn.close()
    p = Scripted([])
    assert cli.run_reindex_all(fake, p, yes=True) == 1
    assert any("worker is running" in s for s in p.said)
    assert _meta(fake)["embedding_provider"] == "gemini"  # nothing changed
