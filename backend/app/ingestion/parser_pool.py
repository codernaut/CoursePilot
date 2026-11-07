"""Run ``parse_document`` in a separate, killable process (CLAUDE.md sections 7.1 and 13).

A thread cannot be stopped, and the cooperative deadline in the parsers only fires between pages,
slides and sections. A pathological file (a hostile PDF stuck inside one pymupdf call, say) could
therefore hang the worker forever. Here the parse runs in a one-process pool (``spawn`` context, so
it behaves the same on Windows and POSIX). When ``timeout_s + grace_s`` elapses the process is
killed, the pool is thrown away and a fresh one is created for the next document. A process that
dies on its own (an out-of-memory kill, a crash in a C extension) is reported the same way.

The child process only receives the file bytes and the limits and returns the parsed sections; it
never touches the database, storage or network.

Not implemented: a memory cap for the child. ``resource.setrlimit`` does not exist on Windows; on
POSIX hosts use the service manager (systemd ``MemoryMax=``) or a container limit for the worker.
Chunking stays in a thread (``asyncio.to_thread``): it is pure Python over the parsed sections.
"""

import asyncio
import contextlib
import multiprocessing
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from typing import Any

from app.core.logging import get_logger
from app.ingestion.parsers import (
    ParsedDocument,
    ParseError,
    ParseLimitError,
    ParseLimits,
    parse_document,
)

log = get_logger(__name__)

ParseFn = Callable[[str, bytes, ParseLimits], ParsedDocument]

DEFAULT_GRACE_S = 10.0  # after the cooperative deadline: let the parser fail with its own message
START_TIMEOUT_S = 120.0


def _noop() -> bool:
    """Runs in the child to start it (and import the parsers) before the clock starts."""
    return True


class ParserPool:
    """At most one parse at a time, in a child process that is killed on timeout."""

    def __init__(
        self,
        parse_fn: ParseFn = parse_document,
        *,
        grace_s: float = DEFAULT_GRACE_S,
        start_timeout_s: float = START_TIMEOUT_S,
    ) -> None:
        self._parse_fn = parse_fn
        self._grace_s = grace_s
        self._start_timeout_s = start_timeout_s
        self._executor: ProcessPoolExecutor | None = None
        self._lock = asyncio.Lock()
        self.restarts = 0  # how many times a child was killed or died (observable in tests)

    async def parse(self, extension: str, data: bytes, limits: ParseLimits) -> ParsedDocument:
        """Parse in the child. Raises ``ParseError`` (``ParseLimitError`` on a timeout)."""
        async with self._lock:
            executor = await self._ensure_started()
            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(executor, self._parse_fn, extension, data, limits)
            hard_limit = limits.timeout_s + self._grace_s
            try:
                return await asyncio.wait_for(future, hard_limit)
            except TimeoutError:
                log.warning("parser_killed_after_timeout", timeout_s=hard_limit)
                self._discard(kill=True)
                raise ParseLimitError(
                    f"Reading this file took longer than {limits.timeout_s:g} seconds, "
                    "so it was stopped."
                ) from None
            except BrokenProcessPool:
                log.warning("parser_process_died")
                self._discard(kill=True)
                raise ParseError(
                    "Reading this file made the parser stop unexpectedly (it may be damaged or "
                    "too large to process)."
                ) from None
            except asyncio.CancelledError:  # worker shutdown: never leave a runaway child behind
                self._discard(kill=True)
                raise

    async def _ensure_started(self) -> ProcessPoolExecutor:
        if self._executor is not None:
            return self._executor
        executor = ProcessPoolExecutor(
            max_workers=1, mp_context=multiprocessing.get_context("spawn")
        )
        self._executor = executor
        try:
            await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(executor, _noop), self._start_timeout_s
            )
        except BaseException:
            self._discard(kill=True)
            raise
        return executor

    def _discard(self, *, kill: bool, count: bool = True) -> None:
        executor, self._executor = self._executor, None
        if executor is None:
            return
        if kill:
            if count:
                self.restarts += 1
            processes: dict[int, Any] = getattr(executor, "_processes", None) or {}
            for process in list(processes.values()):
                with contextlib.suppress(OSError, ValueError):  # already gone
                    process.kill()
        executor.shutdown(wait=False, cancel_futures=True)

    async def aclose(self) -> None:
        self._discard(kill=True, count=False)
