"""LocalStorage: files under ``DATA_DIR/files`` with random keys.

Layout: ``files/<first two key chars>/<key>``. Writes go to ``files/.tmp/<random>.part`` and are
moved into place with ``os.replace`` on commit, so a crash or error never leaves a partial object
under a real key. Only keys matching ``^[0-9a-f]{32}$`` are accepted, and the resolved path is
checked to stay under the storage root.
"""

import asyncio
import os
import re
import secrets
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import BinaryIO

from app.storage.base import (
    READ_CHUNK_SIZE,
    InvalidKeyError,
    Storage,
    StorageError,
    StorageNotFoundError,
    StorageWriter,
)

KEY_PATTERN = re.compile(r"^[0-9a-f]{32}$")
TMP_DIRNAME = ".tmp"


class _LocalWriter(StorageWriter):
    def __init__(self, tmp_path: Path, final_path: Path) -> None:
        self._tmp = tmp_path
        self._final = final_path
        self._file: BinaryIO | None = None
        self._done = False

    def open(self) -> None:
        self._tmp.parent.mkdir(parents=True, exist_ok=True)
        self._file = self._tmp.open("xb")

    def _write(self, data: bytes) -> None:
        if self._file is None:
            raise StorageError("writer is closed")
        self._file.write(data)

    def _commit(self) -> None:
        if self._file is None:
            raise StorageError("writer is closed")
        self._file.flush()
        os.fsync(self._file.fileno())
        self._file.close()
        self._file = None
        if self._final.exists():
            raise StorageError("storage key is already in use")
        self._final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(self._tmp, self._final)
        self._done = True

    def _abort(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        self._tmp.unlink(missing_ok=True)

    async def write(self, data: bytes) -> None:
        await asyncio.to_thread(self._write, data)

    async def commit(self) -> None:
        try:
            await asyncio.to_thread(self._commit)
        except BaseException:
            await self.abort()
            raise

    async def abort(self) -> None:
        if self._done:
            return
        await asyncio.to_thread(self._abort)


class LocalStorage(Storage):
    def __init__(self, data_dir: str | Path) -> None:
        self.root = Path(data_dir) / "files"

    def path_for(self, key: str) -> Path:
        """The on-disk path for ``key``; raises ``InvalidKeyError`` for anything but a real key."""
        if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key):
            raise InvalidKeyError("invalid storage key")
        root = self.root.resolve()
        path = (root / key[:2] / key).resolve()
        if not path.is_relative_to(root) or path == root:
            raise InvalidKeyError("invalid storage key")
        return path

    async def open_writer(self, key: str) -> StorageWriter:
        final = self.path_for(key)
        tmp = self.root.resolve() / TMP_DIRNAME / f"{secrets.token_hex(16)}.part"
        writer = _LocalWriter(tmp, final)
        await asyncio.to_thread(writer.open)
        return writer

    async def read(self, key: str, chunk_size: int = READ_CHUNK_SIZE) -> AsyncIterator[bytes]:
        path = self.path_for(key)
        try:
            handle = await asyncio.to_thread(path.open, "rb")
        except FileNotFoundError:
            raise StorageNotFoundError("object not found") from None
        try:
            while True:
                chunk = await asyncio.to_thread(handle.read, chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            handle.close()

    async def delete(self, key: str) -> None:
        path = self.path_for(key)
        await asyncio.to_thread(path.unlink, True)

    async def exists(self, key: str) -> bool:
        path = self.path_for(key)
        return await asyncio.to_thread(path.is_file)

    async def size(self, key: str) -> int:
        path = self.path_for(key)
        try:
            return (await asyncio.to_thread(path.stat)).st_size
        except FileNotFoundError:
            raise StorageNotFoundError("object not found") from None

    def _check(self) -> None:
        tmp_dir = self.root.resolve() / TMP_DIRNAME
        tmp_dir.mkdir(parents=True, exist_ok=True)
        probe = tmp_dir / f"ready-{secrets.token_hex(8)}.probe"
        try:
            with probe.open("xb") as handle:
                handle.write(b"ok")
        finally:
            probe.unlink(missing_ok=True)

    async def check(self) -> None:
        """Prove the storage directory exists and is writable (readiness probe)."""
        await asyncio.to_thread(self._check)

    def _sweep(self, max_age_s: float) -> int:
        tmp_dir = self.root / TMP_DIRNAME
        if not tmp_dir.is_dir():
            return 0
        cutoff = time.time() - max_age_s
        removed = 0
        for entry in tmp_dir.iterdir():
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    entry.unlink()
                    removed += 1
            except OSError:
                continue  # in use (Windows) or already gone; the next sweep retries
        return removed

    async def sweep_stale_temp(self, max_age_s: float) -> int:
        return await asyncio.to_thread(self._sweep, max_age_s)
