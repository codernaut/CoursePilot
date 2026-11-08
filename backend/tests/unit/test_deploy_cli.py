"""backup / restore / serve / check --production."""

import json
import sqlite3
import subprocess
import webbrowser
from pathlib import Path
from typing import Any

import pytest

from app import cli
from app.core.config import Settings
from app.services import backup, production_check
from app.services.production_check import FAIL, OK, WARN, CheckResult
from app.storage import LocalStorage, StorageError
from tests.conftest import make_settings
from tests.unit.test_setup_flow import Scripted


def db_path(settings: Settings) -> Path:
    return backup.database_path(settings)


def put_file(settings: Settings, name: str, data: bytes) -> Path:
    path = LocalStorage(settings.data_dir).root / name[:2] / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def add_user_row(settings: Settings, email: str) -> None:
    conn = sqlite3.connect(db_path(settings))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "INSERT INTO users (id, email, password_hash, display_name, role, is_active, "
        "must_change_password, created_at, updated_at) "
        "VALUES (?, ?, 'x', 'T', 'student', 1, 0, '2026-01-01', '2026-01-01')",
        (email.replace("@", "")[:32].ljust(32, "0"), email),
    )
    conn.commit()
    conn.close()


def emails(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute("SELECT email FROM users ORDER BY email")]
    finally:
        conn.close()


# --- backup / restore ------------------------------------------------------------------------


def test_backup_is_consistent_verified_and_has_a_manifest(
    settings: Settings, tmp_path: Path
) -> None:
    add_user_row(settings, "wal@example.edu")  # committed, possibly still only in the WAL
    put_file(settings, "ab" * 16, b"stored bytes")
    LocalStorage(settings.data_dir).root.joinpath(".tmp").mkdir(parents=True, exist_ok=True)
    (LocalStorage(settings.data_dir).root / ".tmp" / "x.part").write_bytes(b"partial")
    out = tmp_path / "backups"
    p = Scripted([])
    assert cli.run_backup(settings, p, out=out) == 0
    (folder,) = list(out.iterdir())
    assert folder.name.startswith("coursepilot-backup-")
    assert emails(folder / "coursepilot.db") == ["wal@example.edu"]
    assert (folder / "files" / "ab" / ("ab" * 16)).read_bytes() == b"stored bytes"
    assert not (folder / "files" / ".tmp").exists()
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["file_count"] == 1 and manifest["storage_backend"] == "local"
    assert {"version", "commit", "created_at", "total_bytes"} <= manifest.keys()
    paths = {e["path"] for e in manifest["entries"]}
    assert paths == {"coursepilot.db", f"files/ab/{'ab' * 16}"}
    assert all(len(e["sha256"]) == 64 and e["size"] > 0 for e in manifest["entries"])
    assert backup.verify_backup(folder)["version"] == manifest["version"]
    assert ".env" not in " ".join(paths)


def test_backup_includes_checkpoint_db_when_present(settings: Settings, tmp_path: Path) -> None:
    conn = sqlite3.connect(settings.checkpoint_db_path)
    conn.execute("CREATE TABLE t (a)")
    conn.commit()
    conn.close()
    result = backup.create_backup(settings, tmp_path / "b")
    assert (result.path / "checkpoints.db").is_file()


def test_backup_with_s3_skips_files(tmp_path: Path, settings: Settings) -> None:
    s3 = settings.model_copy(
        update={"storage_backend": "s3"},
    )
    put_file(settings, "cd" * 16, b"x")
    result = backup.create_backup(s3, tmp_path / "b")
    assert result.manifest["files_included"] is False
    assert not (result.path / "files").exists()
    p = Scripted([])
    cli.run_backup(s3, p, out=tmp_path / "b2")
    assert any("S3" in line for line in p.said)


def test_backup_fails_cleanly_without_a_database(tmp_path: Path) -> None:
    settings = make_settings(tmp_path / "empty")
    p = Scripted([])
    assert cli.run_backup(settings, p, out=tmp_path / "b") == 1
    assert any("Backup failed" in line for line in p.said)
    assert list((tmp_path / "b").glob("*")) == []  # no half-written folder


def test_backup_keep_prunes_old_folders(settings: Settings, tmp_path: Path) -> None:
    out = tmp_path / "b"
    for stamp in ("20200101T000000Z", "20200102T000000Z"):
        old = out / f"coursepilot-backup-{stamp}"
        old.mkdir(parents=True)
        (old / "manifest.json").write_text("{}")
    assert cli.run_backup(settings, Scripted([]), out=out, keep=2) == 0
    names = sorted(f.name for f in out.iterdir())
    assert len(names) == 2 and "coursepilot-backup-20200101T000000Z" not in names


def test_restore_round_trip_keeps_the_old_files(settings: Settings, tmp_path: Path) -> None:
    add_user_row(settings, "before@example.edu")
    put_file(settings, "ab" * 16, b"original")
    result = backup.create_backup(settings, tmp_path / "b")
    # damage the live data after the backup
    add_user_row(settings, "after@example.edu")
    put_file(settings, "ab" * 16, b"changed")
    put_file(settings, "ef" * 16, b"new file")

    p = Scripted(["n"])
    assert cli.run_restore(settings, p, result.path) == 1  # declined: nothing changes
    assert "after@example.edu" in emails(db_path(settings))

    assert cli.run_restore(settings, Scripted([]), result.path, yes=True) == 0
    assert emails(db_path(settings)) == ["before@example.edu"]
    files = LocalStorage(settings.data_dir).root
    assert (files / "ab" / ("ab" * 16)).read_bytes() == b"original"
    assert not (files / "ef").exists()
    kept = list(db_path(settings).parent.glob("test.db.pre-restore-*"))
    assert len(kept) == 1 and "after@example.edu" in emails(kept[0])
    assert list(files.parent.glob("files.pre-restore-*"))


def test_restore_refuses_tampered_or_foreign_folders(settings: Settings, tmp_path: Path) -> None:
    result = backup.create_backup(settings, tmp_path / "b")
    with (result.path / "coursepilot.db").open("ab") as handle:
        handle.write(b"tamper")
    p = Scripted([])
    assert cli.run_restore(settings, p, result.path, yes=True) == 1
    assert any("Checksum mismatch" in line for line in p.said)
    empty = tmp_path / "nothing"
    empty.mkdir()
    assert cli.run_restore(settings, Scripted([]), empty, yes=True) == 1


def test_manifest_with_path_escape_is_rejected(settings: Settings, tmp_path: Path) -> None:
    result = backup.create_backup(settings, tmp_path / "b")
    manifest = json.loads((result.path / "manifest.json").read_text())
    manifest["entries"].append({"path": "../../evil", "size": 1, "sha256": "0" * 64})
    (result.path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(backup.BackupError, match="Unsafe"):
        backup.verify_backup(result.path)


def _rewrite_manifest(folder: Path, edit: Any) -> None:
    manifest = json.loads((folder / "manifest.json").read_text())
    edit(manifest)
    (folder / "manifest.json").write_text(json.dumps(manifest))


def test_restore_refuses_a_backup_without_the_main_database_before_touching_live_data(
    settings: Settings, tmp_path: Path
) -> None:
    add_user_row(settings, "live@example.edu")
    result = backup.create_backup(settings, tmp_path / "b")
    (result.path / "coursepilot.db").unlink()
    _rewrite_manifest(
        result.path,
        lambda m: m.update(entries=[e for e in m["entries"] if e["path"] != "coursepilot.db"]),
    )
    assert cli.run_restore(settings, Scripted([]), result.path, yes=True) == 1
    assert emails(db_path(settings)) == ["live@example.edu"]  # still in place, not moved aside
    assert not list(db_path(settings).parent.glob("*.pre-restore-*"))


def test_restore_rejects_files_that_the_manifest_does_not_list(
    settings: Settings, tmp_path: Path
) -> None:
    put_file(settings, "ab" * 16, b"x")
    result = backup.create_backup(settings, tmp_path / "b")
    (result.path / "files" / "ab" / ("cd" * 16)).write_bytes(b"smuggled")
    with pytest.raises(backup.BackupError, match="not listed"):
        backup.verify_backup(result.path)


def test_malformed_manifest_entries_are_a_readable_error(
    settings: Settings, tmp_path: Path
) -> None:
    result = backup.create_backup(settings, tmp_path / "b")
    _rewrite_manifest(result.path, lambda m: m.update(entries=[{"path": "coursepilot.db"}]))
    p = Scripted([])
    assert cli.run_restore(settings, p, result.path, yes=True) == 1
    assert any("Cannot restore" in line for line in p.said)


@pytest.mark.parametrize("keep", [0, -1])
def test_backup_keep_must_be_at_least_one(settings: Settings, tmp_path: Path, keep: int) -> None:
    out = tmp_path / "b"
    p = Scripted([])
    assert cli.run_backup(settings, p, out=out, keep=keep) == 1
    assert not out.exists() or not list(out.iterdir())


def test_cli_parser_accepts_new_commands() -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["backup", "--out", "x", "--keep", "3"]).keep == 3
    assert parser.parse_args(["restore", "folder", "--yes"]).yes is True
    assert parser.parse_args(["check", "--production"]).production is True
    assert parser.parse_args(["serve", "--no-migrate"]).no_migrate is True


# --- serve -----------------------------------------------------------------------------------


def test_serve_runs_only_the_web_app_without_proxy_rewriting(settings: Settings) -> None:
    served: list[dict[str, Any]] = []
    prod = settings.model_copy(
        update={"app_host": "127.0.0.1", "app_port": 8123, "behind_proxy": True}
    )
    code = cli.run_serve(prod, serve=lambda target, **kw: served.append({"target": target, **kw}))
    assert code == 0
    (call,) = served
    assert call["target"] == "app.main:app"
    assert call["host"] == "127.0.0.1" and call["port"] == 8123
    assert call["proxy_headers"] is False and call["access_log"] is False
    assert call["log_config"] is None


def test_serve_migrates_unless_told_not_to(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cli, "run_migrate", lambda _s: calls.append("migrate"))
    cli.run_serve(settings, serve=lambda *a, **k: None)
    cli.run_serve(settings, migrate=False, serve=lambda *a, **k: None)
    assert calls == ["migrate"]


def test_serve_does_not_start_a_worker_or_browser(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_a: object, **_k: object) -> None:
        raise AssertionError("serve must not start a worker or open a browser")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(webbrowser, "open_new_tab", forbidden)
    monkeypatch.setattr(cli, "run_migrate", lambda _s: None)
    cli.run_serve(settings, serve=lambda *a, **k: None)


# --- check --production ----------------------------------------------------------------------


def good(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "production",
        "base_url": "https://study.example.edu",
        "behind_proxy": True,
        "llm_provider": "anthropic",
        "anthropic_api_key": "k",
        "embedding_provider": "gemini",
        "google_api_key": "g",
        "tavily_api_key": "t",
    }
    values.update(overrides)
    return make_settings(tmp_path, **values)


def by_name(results: list[CheckResult]) -> dict[str, CheckResult]:
    return {r.name: r for r in results}


async def run_checks(settings: Settings, **kw: Any) -> dict[str, CheckResult]:
    return by_name(await production_check.run_production_checks(settings, **kw))


async def test_a_good_setup_has_no_failures(tmp_path: Path) -> None:
    results = await production_check.run_production_checks(good(tmp_path), network=lambda _p: False)
    assert [r for r in results if r.status != OK] == []
    lines = production_check.format_results(results)
    assert lines[-1].startswith("0 problem(s), 0 warning(s)")


async def test_dev_defaults_fail(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, secret_key="change-me", base_url="http://localhost:8000")
    results = await run_checks(settings, network=lambda _p: False)
    assert results["APP_ENV"].status == FAIL
    assert results["SECRET_KEY"].status == FAIL
    assert results["BASE_URL"].status == FAIL
    assert results["LLM key"].status == FAIL
    assert results["Embedding key"].status == FAIL
    lines = production_check.format_results(list(results.values()))
    assert "Fix the problems" in lines[-1]


async def test_insecure_http_is_a_warning_not_a_failure(tmp_path: Path) -> None:
    settings = good(
        tmp_path, base_url="http://10.0.0.5:8000", allow_insecure_http=True, behind_proxy=False
    )
    assert (await run_checks(settings, network=lambda _p: False))["BASE_URL"].status == WARN


async def test_proxy_consistency(tmp_path: Path) -> None:
    unflagged = await run_checks(good(tmp_path, behind_proxy=False), network=lambda _p: False)
    assert unflagged["BEHIND_PROXY"].status == WARN
    exposed = await run_checks(
        good(tmp_path, app_host="0.0.0.0"),  # noqa: S104
        network=lambda _p: False,
    )
    assert exposed["BEHIND_PROXY"].status == WARN


async def test_missing_web_key_warns_and_langsmith_warns(tmp_path: Path) -> None:
    settings = good(tmp_path, tavily_api_key=None, langsmith_api_key="ls")
    results = await run_checks(settings, network=lambda _p: False)
    assert results["Web search"].status == WARN
    assert results["LANGSMITH_API_KEY"].status == WARN


async def test_database_on_a_network_share_fails(tmp_path: Path) -> None:
    results = await run_checks(good(tmp_path), network=lambda _p: True)
    assert results["Database"].status == FAIL and results["Checkpoint DB"].status == FAIL


async def test_unreachable_storage_fails(tmp_path: Path) -> None:
    class Broken(LocalStorage):
        async def check(self) -> None:
            raise StorageError("S3 check failed: AccessDenied")

    results = await run_checks(good(tmp_path), storage=Broken(tmp_path), network=lambda _p: False)
    assert results["Storage (local)"].status == FAIL
    assert "AccessDenied" in results["Storage (local)"].message


async def test_unwritable_data_dir_fails(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    settings = good(tmp_path).model_copy(update={"data_dir": str(blocker / "sub")})
    results = await run_checks(settings, network=lambda _p: False)
    assert results["DATA_DIR"].status == FAIL


def test_unc_paths_count_as_network_shares() -> None:
    assert production_check.is_network_path(Path("//server/share/coursepilot.db"))
    assert not production_check.is_network_path(Path(__file__))


def test_cli_check_production_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = Scripted([])
    assert cli.run_production_check(lambda: make_settings(tmp_path), p) == 1
    assert any("[FAIL]" in s for s in p.said)

    def invalid() -> Settings:
        return Settings(_env_file=None, app_env="production", secret_key="short")

    p2 = Scripted([])
    assert cli.run_production_check(invalid, p2) == 1
    assert any("Settings are invalid" in s for s in p2.said)

    monkeypatch.setattr(production_check, "run", lambda _s, storage=None: (["all good"], 0))
    p3 = Scripted([])
    assert cli.run_production_check(lambda: good(tmp_path), p3) == 0
    assert p3.said == ["all good"]


def test_serve_with_worker_starts_and_stops_a_child_worker(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.test_cli import FakeProc

    proc = FakeProc()
    spawned: list[list[str]] = []

    def popen(cmd: list[str], **_kw: object) -> FakeProc:
        spawned.append(cmd)
        return proc

    monkeypatch.setattr(cli, "run_migrate", lambda _s: None)
    cli.run_serve(settings, with_worker=True, serve=lambda *a, **k: None, popen=popen)
    assert spawned and spawned[0][1:] == ["-m", "app.worker"]
    assert proc.stopped_with  # asked to stop when the web app ends
