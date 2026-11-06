"""Storage interface: where uploaded files live. Services depend on this, never on a backend."""

import secrets
from abc import ABC, abstractmethod
from collections.abc import AsyncIterable, AsyncIterator

READ_CHUNK_SIZE = 64 * 1024


class StorageError(Exception):
    """A storage operation failed (bad key, I/O failure, key already used)."""


class StorageNotFoundError(StorageError):
    """The object does not exist."""


class InvalidKeyError(StorageError):
    """The key is not a valid storage key (path traversal attempts end up here)."""


def new_key() -> str:
    """A fresh random storage key (128 bits of hex). Never derived from user input."""
    return secrets.token_hex(16)


class StorageWriter(ABC):
    """An in-progress object. The object only becomes visible under its key on ``commit``;
    ``abort`` (or a crash) leaves nothing behind under that key."""

    @abstractmethod
    async def write(self, data: bytes) -> None: ...

    @abstractmethod
    async def commit(self) -> None: ...

    @abstractmethod
    async def abort(self) -> None:
        """Discard the partial object. Safe to call more than once and after ``commit``."""


class Storage(ABC):
    @abstractmethod
    async def open_writer(self, key: str) -> StorageWriter: ...

    @abstractmethod
    def read(self, key: str, chunk_size: int = READ_CHUNK_SIZE) -> AsyncIterator[bytes]:
        """Stream the object's bytes. Raises ``StorageNotFoundError`` when it is missing."""

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Remove the object. Missing objects are not an error."""

    @abstractmethod
    async def exists(self, key: str) -> bool: ...

    @abstractmethod
    async def size(self, key: str) -> int: ...

    async def check(self) -> None:  # noqa: B027
        """Raise if the storage is unusable (readiness probe). The default does nothing."""

    async def sweep_stale_temp(self, max_age_s: float) -> int:
        """Remove leftovers of interrupted writes older than ``max_age_s``; returns how many.
        Backends without temporary files have nothing to do."""
        return 0

    async def put(self, key: str, chunks: AsyncIterable[bytes]) -> int:
        """Store a stream atomically under ``key``; returns the number of bytes written."""
        writer = await self.open_writer(key)
        total = 0
        try:
            async for chunk in chunks:
                await writer.write(chunk)
                total += len(chunk)
            await writer.commit()
        except BaseException:
            await writer.abort()
            raise
        return total
