"""Command line: ``python -m app.cli <command>``.

Commands: setup, create-admin, seed-structure, migrate, check, start.
"""

import argparse
import asyncio
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from collections.abc import Callable, Sequence
from typing import Any

from app.core.config import REPO_ROOT, Settings, get_settings
from app.core.errors import AppError
from app.db.engine import MIN_SQLITE_VERSION, check_sqlite_capabilities
from app.db.migrate import BACKEND_DIR, upgrade_to_head
from app.db.session import Database
from app.services import structure, users
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

    worker = popen([sys.executable, "-m", "app.worker"], cwd=str(BACKEND_DIR))
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
        )
    except KeyboardInterrupt:
        pass
    finally:
        worker.terminate()
        try:
            worker.wait(timeout=10)
        except subprocess.TimeoutExpired:
            worker.kill()
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
    sub.add_parser("check", help="verify SQLite, FTS5 and sqlite-vec")
    sub.add_parser("start", help="migrate, start the web app and worker, open the browser")
    return parser


def main(argv: Sequence[str] | None = None, *, prompter: Prompter | None = None) -> int:
    args = build_parser().parse_args(argv)
    p = prompter or ConsolePrompter()
    if args.command == "check":
        return run_check(p)
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
    elif args.command == "start":
        return run_start(settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
