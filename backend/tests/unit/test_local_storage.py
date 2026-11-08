from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.core.config import Settings
from app.storage import (
    InvalidKeyError,
    LocalStorage,
    S3Storage,
    StorageError,
    StorageNotFoundError,
    get_storage,
    new_key,
)
from tests.conftest import make_settings


async def chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


def stored_files(storage: LocalStorage) -> list[Path]:
    return [p for p in storage.root.rglob("*") if p.is_file()] if storage.root.exists() else []


async def test_put_read_exists_size_delete(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    key = new_key()
    assert not await storage.exists(key)
    written = await storage.put(key, chunks(b"hello ", b"world"))
    assert written == 11
    assert await storage.exists(key)
    assert await storage.size(key) == 11
    assert b"".join([c async for c in storage.read(key, chunk_size=4)]) == b"hello world"
    await storage.delete(key)
    assert not await storage.exists(key)
    await storage.delete(key)  # deleting a missing object is not an error
    assert stored_files(storage) == []


def test_new_key_is_random_hex() -> None:
    keys = {new_key() for _ in range(50)}
    assert len(keys) == 50
    assert all(len(k) == 32 and set(k) <= set("0123456789abcdef") for k in keys)


async def test_files_live_under_data_dir_files(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    key = new_key()
    await storage.put(key, chunks(b"x"))
    assert storage.path_for(key) == (tmp_path / "files" / key[:2] / key).resolve()
    assert storage.path_for(key).read_bytes() == b"x"


async def test_failed_write_leaves_no_partial_object(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    key = new_key()

    async def broken() -> AsyncIterator[bytes]:
        yield b"partial data"
        raise RuntimeError("connection lost")

    with pytest.raises(RuntimeError, match="connection lost"):
        await storage.put(key, broken())
    assert not await storage.exists(key)
    assert stored_files(storage) == []  # the temp file is gone too


async def test_object_is_invisible_until_commit(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    key = new_key()
    writer = await storage.open_writer(key)
    await writer.write(b"abc")
    assert not await storage.exists(key)
    await writer.commit()
    assert await storage.exists(key)
    await writer.abort()  # after commit: harmless, keeps the object
    assert await storage.exists(key)


async def test_abort_discards_the_object(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    key = new_key()
    writer = await storage.open_writer(key)
    await writer.write(b"abc")
    await writer.abort()
    await writer.abort()
    assert not await storage.exists(key)
    assert stored_files(storage) == []


async def test_existing_key_is_not_overwritten(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    key = new_key()
    await storage.put(key, chunks(b"first"))
    with pytest.raises(StorageError):
        await storage.put(key, chunks(b"second"))
    assert b"".join([c async for c in storage.read(key)]) == b"first"
    assert len(stored_files(storage)) == 1


async def test_read_and_size_of_missing_object(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    key = new_key()
    with pytest.raises(StorageNotFoundError):
        [c async for c in storage.read(key)]
    with pytest.raises(StorageNotFoundError):
        await storage.size(key)


BAD_KEYS = [
    "",
    "..",
    "../secret",
    "../../etc/passwd",
    "a" * 31,
    "a" * 33,
    "A" * 32,  # upper case is not a generated key
    "g" * 32,
    "a" * 31 + "/",
    "a" * 30 + "/a",
    "a" * 30 + "\\a",
    "/" + "a" * 31,
    "a" * 32 + "\n",
    "a" * 32 + "\x00",
    "..%2f" + "a" * 27,
    "C:\\Windows\\win.ini",
    "user-chosen-file-name.pdf",
]


@pytest.mark.parametrize("key", BAD_KEYS)
async def test_invalid_keys_are_rejected_everywhere(tmp_path: Path, key: str) -> None:
    storage = LocalStorage(tmp_path)
    with pytest.raises(InvalidKeyError):
        storage.path_for(key)
    with pytest.raises(InvalidKeyError):
        await storage.open_writer(key)
    with pytest.raises(InvalidKeyError):
        await storage.exists(key)
    with pytest.raises(InvalidKeyError):
        await storage.delete(key)
    with pytest.raises(InvalidKeyError):
        await storage.size(key)
    with pytest.raises(InvalidKeyError):
        [c async for c in storage.read(key)]


async def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "data")
    key = "ab" + "c" * 30
    outside = tmp_path / "outside"
    outside.mkdir()
    storage.root.mkdir(parents=True)
    try:
        (storage.root / "ab").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    with pytest.raises(InvalidKeyError):
        storage.path_for(key)


def test_get_storage_selects_by_backend(tmp_path: Path) -> None:
    assert isinstance(get_storage(make_settings(tmp_path)), LocalStorage)
    s3: Settings = make_settings(
        tmp_path, storage_backend="s3", s3_access_key_id="k", s3_secret_access_key="s"
    )
    assert isinstance(get_storage(s3), S3Storage)


async def test_sweep_removes_only_stale_temp_files(tmp_path: Path) -> None:
    import os
    import time

    storage = LocalStorage(tmp_path)
    key = new_key()
    await storage.put(key, chunks(b"kept"))
    writer = await storage.open_writer(new_key())  # an in-flight upload: fresh temp file
    await writer.write(b"in flight")
    stale = storage.root / ".tmp" / "deadbeef.part"
    stale.write_bytes(b"leftover from a crash")
    old = time.time() - 2 * 3600
    os.utime(stale, (old, old))

    assert await storage.sweep_stale_temp(max_age_s=3600) == 1
    assert not stale.exists()
    assert await storage.exists(key)  # committed objects are never touched
    await writer.commit()  # the fresh temp file survived the sweep
    assert await storage.sweep_stale_temp(max_age_s=3600) == 0


async def test_sweep_and_check_work_on_a_fresh_data_dir(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "nothing-yet")
    assert await storage.sweep_stale_temp(max_age_s=1) == 0
    await storage.check()  # creates the root and proves it is writable
    assert (tmp_path / "nothing-yet" / "files").is_dir()
    assert stored_files(storage) == []  # the probe file is gone again
