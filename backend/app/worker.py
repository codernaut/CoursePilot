"""Background worker entry point: ``python -m app.worker``.

M1: purges expired sessions and old audit rows periodically. The job queue (ingestion, re-index,
delete) is added in M3 and plugs into the same loop.
"""

import asyncio
import contextlib
import time

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.core.startup import warn_if_insecure_http
from app.db.session import Database
from app.services import audit, auth
from app.storage import Storage, get_storage

log = get_logger(__name__)

SESSION_PURGE_INTERVAL_S = 300.0
AUDIT_PURGE_INTERVAL_S = 3600.0
STALE_TEMP_MAX_AGE_S = 3600.0  # leftovers of interrupted uploads


async def purge_once(database: Database) -> int:
    async with database.session() as session:
        return await auth.purge_expired_sessions(session)


async def purge_audit_once(database: Database, settings: Settings) -> int:
    async with database.session() as session:
        return await audit.purge_old(
            session,
            retention_days=settings.audit_retention_days,
            login_failed_retention_days=settings.login_failed_audit_retention_days,
        )


async def sweep_storage_once(storage: Storage) -> int:
    return await storage.sweep_stale_temp(STALE_TEMP_MAX_AGE_S)


async def run_worker(
    settings: Settings,
    stop: asyncio.Event,
    *,
    interval_s: float = SESSION_PURGE_INTERVAL_S,
    audit_interval_s: float = AUDIT_PURGE_INTERVAL_S,
) -> None:
    """Run until ``stop`` is set. Returns promptly when it is."""
    database = Database(settings.database_url)
    warn_if_insecure_http(settings)
    storage = get_storage(settings)
    log.info("worker_started")
    last_audit_purge: float | None = None
    try:
        while not stop.is_set():
            try:
                purged = await purge_once(database)
                if purged:
                    log.info("expired_sessions_purged", count=purged)
            except Exception as exc:  # keep the worker alive; the next tick retries
                log.warning("purge_failed", error=type(exc).__name__)
            try:  # runs at startup (first iteration) and on every tick
                swept = await sweep_storage_once(storage)
                if swept:
                    log.info("stale_upload_temp_files_removed", count=swept)
            except Exception as exc:
                log.warning("storage_sweep_failed", error=type(exc).__name__)
            now = time.monotonic()
            if last_audit_purge is None or now - last_audit_purge >= audit_interval_s:
                try:
                    removed = await purge_audit_once(database, settings)
                    last_audit_purge = now
                    if removed:
                        log.info("audit_rows_purged", count=removed)
                except Exception as exc:
                    log.warning("audit_purge_failed", error=type(exc).__name__)
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
