"""File storage: a ``Storage`` interface with a local-disk implementation (S3 comes in M8)."""

from app.core.config import Settings
from app.storage.base import (
    InvalidKeyError,
    Storage,
    StorageError,
    StorageNotFoundError,
    StorageWriter,
    new_key,
)
from app.storage.local import LocalStorage


def get_storage(settings: Settings) -> Storage:
    if settings.storage_backend == "local":
        return LocalStorage(settings.data_dir)
    raise NotImplementedError(
        "STORAGE_BACKEND=s3 is not implemented until M8; set STORAGE_BACKEND=local."
    )


__all__ = [
    "InvalidKeyError",
    "LocalStorage",
    "Storage",
    "StorageError",
    "StorageNotFoundError",
    "StorageWriter",
    "get_storage",
    "new_key",
]
