"""Command line: ``python -m app.cli <command>``.

Commands: setup, create-admin, seed-structure, migrate, check, start, serve, reindex-all, backup,
restore.
"""

import argparse
import asyncio
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from app.core.config import REPO_ROOT, Settings, get_settings
from app.core.errors import AppError
from app.db.engine import MIN_SQLITE_VERSION, check_sqlite_capabilities
from app.db.migrate import BACKEND_DIR, upgrade_to_head
from app.db.session import Database
from app.services import backup, production_check, structure, users
from app.setup_flow import ConsolePrompter, Prompter, SetupDeps, prompt_admin_details, run_setup


def run_check(p: Prompter) -> int:
    import sqlite3

    p.say(f"SQLite {sqlite3.sqlite_version} (need >= {'.'.join(map(str, MIN_SQLITE_VERSION))})")
    problems = check_sqlite_capabilities()
    if problems:
        for problem in problems:
            p.say(f"PROBLEM: {problem}")
        return 1
    p.say("OK: SQLite version, FTS5 and sqlite-vec all work.")
    return 0


def run_migrate(settings: Settings) -> None:
    upgrade_to_head(settings)


def run_seed(settings: Settings) -> int:
    async def _seed() -> int:
        database = Database(settings.database_url)
        try:
            async with database.session() as session:
                result = await structure.seed_structure(
                    session,
                    years=settings.program_years,
                    blocks_per_year=settings.blocks_per_year,
                    modules_per_block=settings.modules_per_block,
                )
            return result.total
        finally:
            await database.dispose()

    return asyncio.run(_seed())


def _admin_exists(settings: Settings) -> bool:
    async def _check() -> bool:
        database = Database(settings.database_url)
        try:
            async with database.read_session() as session:
                return await users.admin_exists(session)
        finally:
            await database.dispose()

    return asyncio.run(_check())


def _create_admin(settings: Settings, email: str, display_name: str, password: str) -> None:
    async def _create() -> None:
        database = Database(settings.database_url)
        try:
            async with database.session() as session:
                await users.create_initial_admin(
                    session, email=email, display_name=display_name, password=password
                )
        finally:
            await database.dispose()

    asyncio.run(_create())


def run_create_admin(
    settings: Settings,
    p: Prompter,
    *,
    email: str | None = None,
    create: Callable[[Settings, str, str, str], None] = _create_admin,
) -> int:
    details = prompt_admin_details(p, email)
    if details is None:
        return 1
    email, name, password = details
    try:
        create(settings, email, name, password)
    except AppError as exc:
        p.say(f"Could not create the administrator: {exc.message}")
        return 1
    p.say(f"Created administrator {email.strip().lower()}.")
    return 0


def run_reindex_all(
    settings: Settings, p: Prompter, *, yes: bool = False, database: Database | None = None
) -> int:
    """Rebuild the vector index after the embedding provider, model or dimension changed."""
    from app.ingestion.embedder import EmbeddingNotConfiguredError, get_embedder
    from app.ingestion.index_meta import config_from_settings
    from app.services import reindex

    try:
        get_embedder(settings)
    except EmbeddingNotConfiguredError as exc:
        p.say(f"Cannot re-index: {exc.message}")
        return 1
    config = config_from_settings(settings)

    async def _run() -> int:
        db = database or Database(settings.database_url)
        try:
            total = await reindex.count_documents(db)
            p.say(
                f"This drops the search vectors and re-embeds {total} document(s) with "
                f"{config.describe()}. Documents are not searchable until the worker has "
                "processed them again (this can take a while and uses the embedding service)."
            )
            p.say("Stop the worker first if it is running, and start it again afterwards.")
            if not yes and p.ask("Continue? [y/N]:").strip().lower() not in ("y", "yes"):
                p.say("Cancelled. Nothing was changed.")
                return 1
            try:
                summary = await reindex.reindex_all(db, settings)
            except reindex.WorkerBusyError:
                p.say(
                    "Cannot re-index: a worker is running a job right now. Stop the worker "
                    "(or wait until it is idle and stop it), then run this again."
                )
                return 1
            p.say(
                f"Done: {summary.documents} document(s) queued for re-indexing "
                f"({summary.jobs_enqueued} new job(s)). Start the worker to process them."
            )
            return 0
        finally:
            if database is None:
                await db.dispose()

    return asyncio.run(_run())


def run_production_check(settings_loader: Callable[[], Settings], p: Prompter) -> int:
    """``check --production``: the go-live checklist. Invalid settings count as a failure."""
    from pydantic import ValidationError

    try:
        settings = settings_loader()
    except ValidationError as exc:
        p.say("[FAIL] Settings are invalid, the app would not start:")
        for error in exc.errors():
            p.say(f"       {'.'.join(str(x) for x in error['loc']) or 'settings'}: {error['msg']}")
        return 1
    lines, code = production_check.run(settings)
    for line in lines:
        p.say(line)
    return code


def run_serve(
    settings: Settings,
    *,
    migrate: bool = True,
    with_worker: bool = False,
    serve: Callable[..., Any] | None = None,
    popen: Callable[..., Any] = subprocess.Popen,
) -> int:
    """Production web process, no browser. Run ``python -m app.worker`` separately, or pass
    ``with_worker`` to run it as a child process (single-container hosts with one disk).

    uvicorn's own proxy-header rewriting stays off: the app reads X-Forwarded-For itself, and only
    when BEHIND_PROXY is set (``api.deps.client_ip``).
    """
    if migrate:
        run_migrate(settings)
    if serve is None:
        import uvicorn

        serve = uvicorn.run
    worker = None
    if with_worker:
        worker = popen(
            [sys.executable, "-m", "app.worker"], cwd=str(BACKEND_DIR), **worker_popen_options()
        )
    try:
        serve(
            "app.main:app",
            host=settings.app_host,
            port=settings.app_port,
            log_level=settings.log_level.lower(),
            proxy_headers=False,
            access_log=False,
            log_config=None,
        )
    except KeyboardInterrupt:
        pass
    finally:
        if worker is not None:
            stop_worker(worker)
    return 0


def prune_backups(base: Path, keep: int) -> list[Path]:
    """Delete the oldest ``coursepilot-backup-*`` folders beyond the newest ``keep``."""
    import shutil

    folders = sorted(
        (f for f in base.glob("coursepilot-backup-*") if (f / backup.MANIFEST_NAME).is_file()),
        key=lambda f: f.name,
    )
    doomed = folders[: max(0, len(folders) - keep)]
    for folder in doomed:
        shutil.rmtree(folder, ignore_errors=True)
    return doomed


def run_backup(
    settings: Settings, p: Prompter, *, out: Path | None = None, keep: int | None = None
) -> int:
    if keep is not None and keep < 1:
        p.say("--keep must be at least 1.")
        return 1
    try:
        result = backup.create_backup(settings, out)
    except (backup.BackupError, OSError) as exc:
        p.say(f"Backup failed: {exc}")
        return 1
    manifest = result.manifest
    p.say(
        f"Backup written to {result.path} ({manifest['total_bytes']} bytes, "
        f"{manifest['file_count']} uploaded file(s))."
    )
    if manifest["storage_backend"] != "local":
        p.say("Uploaded files are in S3 storage and are NOT part of this backup.")
    p.say("The .env file (secrets) is not included; keep a copy of it somewhere safe.")
    if keep is not None:
        for old in prune_backups(result.path.parent, keep):
            p.say(f"Removed old backup {old.name}.")
    return 0


def run_restore(settings: Settings, p: Prompter, folder: Path, *, yes: bool = False) -> int:
    try:
        manifest = backup.verify_backup(folder)
    except (backup.BackupError, OSError) as exc:
        p.say(f"Cannot restore: {exc}")
        return 1
    p.say(
        f"Backup of version {manifest.get('version')} made {manifest.get('created_at')} "
        f"({manifest.get('file_count')} uploaded file(s)); all checksums match."
    )
    p.say(
        "This replaces the current databases and uploaded files. "
        "Stop the web app AND the worker first."
    )
    p.say("The current files are kept next to the new ones as <name>.pre-restore-<timestamp>.")
    if not yes and p.ask("Restore now? [y/N]:").strip().lower() not in ("y", "yes"):
        p.say("Cancelled. Nothing was changed.")
        return 1
    try:
        done = backup.restore_backup(settings, folder)
    except (backup.BackupError, OSError) as exc:
        p.say(f"Restore failed: {exc}")
        return 1
    for line in done:
        p.say(line)
    p.say("Done. Start the app and the worker; migrations run on start.")
    return 0


def default_setup_deps() -> SetupDeps:
    def migrate(settings: Settings) -> None:
        from pathlib import Path

        Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
        run_migrate(settings)

    return SetupDeps(
        migrate=migrate,
        seed=run_seed,
        admin_exists=_admin_exists,
        create_admin=_create_admin,
        check_sqlite=check_sqlite_capabilities,
    )


WORKER_STOP_GRACE_S = 25.0  # a little more than the worker's own 20 s shutdown grace


def worker_popen_options(*, windows: bool | None = None) -> dict[str, Any]:
    """Start the worker in its own process group / session: Windows can only send it a graceful
    CTRL_BREAK_EVENT that way, and a console Ctrl+C no longer kills it behind our back."""
    if windows is None:
        windows = sys.platform == "win32"
    if windows:
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def stop_worker(
    worker: Any, *, windows: bool | None = None, grace_s: float = WORKER_STOP_GRACE_S
) -> None:
    """Ask the worker to stop (it finishes or releases its job), wait ``grace_s``, then kill it."""
    if windows is None:
        windows = sys.platform == "win32"
    if worker.poll() is not None:
        return
    try:
        if windows:
            worker.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
        else:
            worker.terminate()
    except (OSError, ValueError):  # already gone, or no console to signal
        worker.terminate()
    try:
        worker.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        worker.kill()
        worker.wait(timeout=5)


def run_start(
    settings: Settings,
    *,
    popen: Callable[..., Any] = subprocess.Popen,
    serve: Callable[..., Any] | None = None,
    open_browser: Callable[[str], Any] = webbrowser.open_new_tab,
    wait_until_ready: Callable[[str], bool] | None = None,
) -> int:
    """Migrate, start the worker process and the web app, open the browser."""
    run_migrate(settings)
    if serve is None:
        import uvicorn

        serve = uvicorn.run
    ready = wait_until_ready or _wait_for_health
    host = "localhost" if settings.app_host in ("0.0.0.0", "::") else settings.app_host  # noqa: S104
    url = f"http://{host}:{settings.app_port}"

    worker = popen(
        [sys.executable, "-m", "app.worker"], cwd=str(BACKEND_DIR), **worker_popen_options()
    )
    try:
        threading.Thread(
            target=lambda: ready(url) and open_browser(url), daemon=True, name="open-browser"
        ).start()
        print(f"CoursePilot is starting at {url} (Ctrl+C to stop)")
        serve(
            "app.main:app",
            host=settings.app_host,
            port=settings.app_port,
            log_level=settings.log_level.lower(),
            proxy_headers=False,
            access_log=False,  # replaced by our structured access log
            log_config=None,  # keep our logging configuration (JSON, redaction)
        )
    except KeyboardInterrupt:
        pass
    finally:
        stop_worker(worker)
    return 0


def _wait_for_health(url: str, timeout_s: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/api/v1/health/live", timeout=2) as response:  # noqa: S310
                if response.status == 200:
                    return True
        except OSError:
            time.sleep(0.5)
    return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="CoursePilot commands")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="interactive first-time setup (writes .env, migrates, seeds)")
    admin = sub.add_parser("create-admin", help="create an administrator (password is prompted)")
    admin.add_argument("--email", help="email address (prompted when omitted)")
    sub.add_parser("seed-structure", help="create missing years, blocks and modules")
    sub.add_parser("migrate", help="apply database migrations")
    check = sub.add_parser("check", help="verify SQLite, FTS5 and sqlite-vec")
    check.add_argument(
        "--production",
        action="store_true",
        help="also print the go-live checklist (exit 1 on failures)",
    )
    sub.add_parser("start", help="migrate, start the web app and worker, open the browser")
    serve = sub.add_parser(
        "serve", help="production: migrate and run the web app only (no browser)"
    )
    serve.add_argument("--no-migrate", action="store_true", help="skip the migration step")
    serve.add_argument(
        "--with-worker", action="store_true", help="also run the worker as a child process"
    )
    backup_cmd = sub.add_parser(
        "backup", help="consistent backup of the databases and uploaded files"
    )
    backup_cmd.add_argument(
        "--out", type=Path, help="folder for backups (default DATA_DIR/backups)"
    )
    backup_cmd.add_argument(
        "--keep", type=int, help="keep only the newest N backups in that folder"
    )
    restore_cmd = sub.add_parser("restore", help="restore a backup (stop the app and worker first)")
    restore_cmd.add_argument("folder", type=Path, help="a coursepilot-backup-* folder")
    restore_cmd.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    reindex = sub.add_parser(
        "reindex-all",
        help="rebuild the vector index after changing EMBEDDING_PROVIDER/MODEL/DIM",
    )
    reindex.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    return parser


def main(argv: Sequence[str] | None = None, *, prompter: Prompter | None = None) -> int:
    args = build_parser().parse_args(argv)
    p = prompter or ConsolePrompter()
    if args.command == "check":
        code = run_check(p)
        if args.production:
            code = max(code, run_production_check(get_settings, p))
        return code
    if args.command == "setup":
        return run_setup(
            p,
            default_setup_deps(),
            env_path=REPO_ROOT / ".env",
            example_path=REPO_ROOT / ".env.example",
        )
    settings = get_settings()
    if args.command == "migrate":
        run_migrate(settings)
        p.say("Database is up to date.")
    elif args.command == "seed-structure":
        run_migrate(settings)
        p.say(f"Created {run_seed(settings)} new structure rows.")
    elif args.command == "create-admin":
        run_migrate(settings)
        return run_create_admin(settings, p, email=args.email)
    elif args.command == "reindex-all":
        run_migrate(settings)
        return run_reindex_all(settings, p, yes=args.yes)
    elif args.command == "start":
        return run_start(settings)
    elif args.command == "serve":
        return run_serve(settings, migrate=not args.no_migrate, with_worker=args.with_worker)
    elif args.command == "backup":
        return run_backup(settings, p, out=args.out, keep=args.keep)
    elif args.command == "restore":
        return run_restore(settings, p, args.folder, yes=args.yes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
