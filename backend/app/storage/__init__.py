"""File storage: a ``Storage`` interface with local-disk and S3-compatible implementations."""

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
from app.storage.s3 import S3Storage


def get_storage(settings: Settings) -> Storage:
    if settings.storage_backend == "local":
        return LocalStorage(settings.data_dir)
    return S3Storage.from_settings(settings)


__all__ = [
    "InvalidKeyError",
    "LocalStorage",
    "S3Storage",
    "Storage",
    "StorageError",
    "StorageNotFoundError",
    "StorageWriter",
    "get_storage",
    "new_key",
]
