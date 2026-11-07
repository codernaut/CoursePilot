"""Background worker entry point: ``python -m app.worker``.

One loop does two things:

* claims and runs queued jobs (``ingest_document``, ``reindex_document``, ``delete_document``) one
  at a time through ``JobRunner``; parsing and chunking run in a thread, embedding is async I/O;
* periodic housekeeping: expired sessions, audit retention, stale upload temp files.

Shutdown (Ctrl+C, SIGTERM, or ``stop`` being set): the running job gets a grace period to finish;
otherwise it is cancelled and released back to the queue. A worker that is killed outright leaves a
``running`` job behind, which the queue re-queues once its heartbeat is older than
``HEARTBEAT_STALE_S`` (a live worker refreshes it every quarter of that).
"""

import asyncio
import contextlib
import os
import signal
import socket
import time
from collections.abc import Callable

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.core.startup import warn_if_insecure_http
from app.db.session import Database
from app.ingestion.embedder import Embedder, get_embedder
from app.ingestion.handlers import build_failure_hook, build_handlers, build_release_hook
from app.ingestion.pipeline import PipelineDeps
from app.jobs import JobRunner, SqliteJobQueue
from app.services import audit, auth
from app.storage import Storage, get_storage

log = get_logger(__name__)

SESSION_PURGE_INTERVAL_S = 300.0
AUDIT_PURGE_INTERVAL_S = 3600.0
STALE_TEMP_MAX_AGE_S = 3600.0  # leftovers of interrupted uploads
SHUTDOWN_GRACE_S = 20.0


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


def stale_after_s(settings: Settings) -> float:
    """A running job whose heartbeat is older than this is taken over: ``HEARTBEAT_STALE_S``, never
    longer than ``JOB_LOCK_TIMEOUT_S``."""
    return float(min(settings.heartbeat_stale_s, settings.job_lock_timeout_s))


def heartbeat_interval_s(settings: Settings) -> float:
    """Four heartbeats per stale window, so a live worker is never mistaken for a dead one."""
    return max(1.0, stale_after_s(settings) / 4)


def make_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{os.urandom(2).hex()}"


async def run_worker(
    settings: Settings,
    stop: asyncio.Event,
    *,
    interval_s: float = SESSION_PURGE_INTERVAL_S,
    audit_interval_s: float = AUDIT_PURGE_INTERVAL_S,
    poll_interval_s: float | None = None,
    embedder_factory: Callable[[], Embedder] | None = None,
    storage: Storage | None = None,
    shutdown_grace_s: float = SHUTDOWN_GRACE_S,
) -> None:
    """Run until ``stop`` is set. Returns promptly when it is."""
    database = Database(settings.database_url)
    warn_if_insecure_http(settings)
    storage = storage or get_storage(settings)
    poll = poll_interval_s if poll_interval_s is not None else settings.worker_poll_interval_s
    worker_id = make_worker_id()
    deps = PipelineDeps(
        database=database,
        storage=storage,
        settings=settings,
        embedder_factory=embedder_factory or (lambda: get_embedder(settings)),
    )
    queue = SqliteJobQueue(database, lock_timeout_s=stale_after_s(settings))
    runner = JobRunner(
        queue,
        build_handlers(deps),
        worker_id,
        heartbeat_interval_s=heartbeat_interval_s(settings),
        on_failure=build_failure_hook(deps),
        on_release=build_release_hook(deps),
    )
    log.info("worker_started", worker_id=worker_id)
    last_housekeeping: float | None = None
    last_audit_purge: float | None = None
    try:
        while not stop.is_set():
            now = time.monotonic()
            if last_housekeeping is None or now - last_housekeeping >= interval_s:
                last_housekeeping = now
                try:
                    purged = await purge_once(database)
                    if purged:
                        log.info("expired_sessions_purged", count=purged)
                except Exception as exc:  # keep the worker alive; the next tick retries
                    log.warning("purge_failed", error=type(exc).__name__)
                try:
                    swept = await sweep_storage_once(storage)
                    if swept:
                        log.info("stale_upload_temp_files_removed", count=swept)
                except Exception as exc:
                    log.warning("storage_sweep_failed", error=type(exc).__name__)
            if last_audit_purge is None or now - last_audit_purge >= audit_interval_s:
                try:
                    removed = await purge_audit_once(database, settings)
                    last_audit_purge = now
                    if removed:
                        log.info("audit_rows_purged", count=removed)
                except Exception as exc:
                    log.warning("audit_purge_failed", error=type(exc).__name__)

            try:
                worked = await runner.run_next(stop, grace_s=shutdown_grace_s)
            except Exception as exc:  # e.g. the database was briefly unavailable
                log.warning("job_loop_failed", error=type(exc).__name__)
                worked = False
            if worked:
                continue  # more may be waiting; housekeeping is time-gated above
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=min(poll, interval_s))
    finally:
        await database.dispose()
        log.info("worker_stopped")


def install_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    """SIGINT/SIGTERM (and SIGBREAK on Windows) set ``stop`` so the current job can finish."""

    def request_stop() -> None:
        loop.call_soon_threadsafe(stop.set)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError, ValueError):  # Windows event loops
            with contextlib.suppress(ValueError, OSError):  # not the main thread
                signal.signal(sig, lambda *_: request_stop())


async def _amain(settings: Settings) -> None:
    stop = asyncio.Event()
    install_signal_handlers(asyncio.get_running_loop(), stop)
    await run_worker(settings, stop)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.app_env != "development")
    with contextlib.suppress(KeyboardInterrupt):  # Ctrl+C is a normal way to stop the worker
        asyncio.run(_amain(settings))


if __name__ == "__main__":
    main()
