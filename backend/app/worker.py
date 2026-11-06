"""Background worker entry point: ``python -m app.worker``.

M1: only purges expired sessions periodically. The job queue (ingestion, re-index, delete) is
added in M3 and plugs into the same loop.
"""

import asyncio
import contextlib

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import Database
from app.services import auth

log = get_logger(__name__)

SESSION_PURGE_INTERVAL_S = 300.0


async def purge_once(database: Database) -> int:
    async with database.session() as session:
        return await auth.purge_expired_sessions(session)


async def run_worker(
    settings: Settings,
    stop: asyncio.Event,
    *,
    interval_s: float = SESSION_PURGE_INTERVAL_S,
) -> None:
    """Run until ``stop`` is set. Returns promptly when it is."""
    database = Database(settings.database_url)
    log.info("worker_started")
    try:
        while not stop.is_set():
            try:
                purged = await purge_once(database)
                if purged:
                    log.info("expired_sessions_purged", count=purged)
            except Exception as exc:  # keep the worker alive; the next tick retries
                log.warning("purge_failed", error=type(exc).__name__)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval_s)
    finally:
        await database.dispose()
        log.info("worker_stopped")


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.app_env != "development")
    stop = asyncio.Event()
    with contextlib.suppress(KeyboardInterrupt):  # Ctrl+C is a normal way to stop the worker
        asyncio.run(run_worker(settings, stop))


if __name__ == "__main__":
    main()
