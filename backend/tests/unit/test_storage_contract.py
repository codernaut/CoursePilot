"""One contract suite, run against LocalStorage and S3Storage (moto), plus S3-only behaviour."""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from app.storage import (
    InvalidKeyError,
    LocalStorage,
    S3Storage,
    Storage,
    StorageError,
    StorageNotFoundError,
    new_key,
)

BUCKET = "test-bucket"
MIB = 1024 * 1024


async def chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


async def read_all(storage: Storage, key: str, chunk_size: int = 64 * 1024) -> bytes:
    return b"".join([c async for c in storage.read(key, chunk_size=chunk_size)])


@pytest.fixture
def moto_s3() -> Iterator[Any]:
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


def make_s3(**kwargs: Any) -> S3Storage:
    return S3Storage(
        bucket=kwargs.pop("bucket", BUCKET),
        region="us-east-1",
        access_key_id="test",
        secret_access_key="test",
        **kwargs,
    )


@pytest.fixture(params=["local", "s3"])
def storage(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Storage]:
    if request.param == "local":
        yield LocalStorage(tmp_path)
        return
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        yield make_s3()


# --- contract (both backends) ----------------------------------------------------------------


async def test_put_read_exists_size_delete(storage: Storage) -> None:
    key = new_key()
    assert not await storage.exists(key)
    assert await storage.put(key, chunks(b"hello ", b"world")) == 11
    assert await storage.exists(key)
    assert await storage.size(key) == 11
    assert await read_all(storage, key, chunk_size=4) == b"hello world"
    await storage.delete(key)
    assert not await storage.exists(key)
    await storage.delete(key)  # deleting a missing object is not an error


async def test_empty_object(storage: Storage) -> None:
    key = new_key()
    assert await storage.put(key, chunks()) == 0
    assert await storage.size(key) == 0
    assert await read_all(storage, key) == b""


async def test_missing_object_errors(storage: Storage) -> None:
    key = new_key()
    with pytest.raises(StorageNotFoundError):
        await storage.size(key)
    with pytest.raises(StorageNotFoundError):
        await read_all(storage, key)


async def test_writer_is_invisible_until_commit_and_abort_leaves_nothing(storage: Storage) -> None:
    key = new_key()
    writer = await storage.open_writer(key)
    await writer.write(b"partial")
    assert not await storage.exists(key)
    await writer.abort()
    await writer.abort()  # idempotent
    assert not await storage.exists(key)


async def test_failed_stream_leaves_no_object(storage: Storage) -> None:
    key = new_key()

    async def broken() -> AsyncIterator[bytes]:
        yield b"some data"
        raise RuntimeError("client went away")

    with pytest.raises(RuntimeError):
        await storage.put(key, broken())
    assert not await storage.exists(key)


async def test_abort_after_commit_keeps_the_object(storage: Storage) -> None:
    key = new_key()
    writer = await storage.open_writer(key)
    await writer.write(b"data")
    await writer.commit()
    await writer.abort()
    assert await read_all(storage, key) == b"data"


async def test_existing_key_is_not_overwritten(storage: Storage) -> None:
    key = new_key()
    await storage.put(key, chunks(b"first"))
    with pytest.raises(StorageError):
        await storage.put(key, chunks(b"second"))
    assert await read_all(storage, key) == b"first"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "../etc/passwd",
        "a/b",
        "A" * 32,
        "g" * 32,
        "ab" * 15,
        "ab" * 17,
        "/abs/path",
        "a" * 31 + "\n",
    ],
)
async def test_invalid_keys_are_rejected_everywhere(storage: Storage, bad: str) -> None:
    with pytest.raises(InvalidKeyError):
        await storage.open_writer(bad)
    with pytest.raises(InvalidKeyError):
        await storage.exists(bad)
    with pytest.raises(InvalidKeyError):
        await storage.size(bad)
    with pytest.raises(InvalidKeyError):
        await storage.delete(bad)
    with pytest.raises(InvalidKeyError):
        await read_all(storage, bad)


async def test_check_passes_and_sweep_is_harmless(storage: Storage) -> None:
    await storage.check()
    assert await storage.sweep_stale_temp(3600) == 0


# --- S3 only ---------------------------------------------------------------------------------


async def test_s3_object_layout_uses_the_prefix(moto_s3: Any) -> None:
    storage = make_s3()
    key = new_key()
    await storage.put(key, chunks(b"x"))
    names = [o["Key"] for o in moto_s3.list_objects_v2(Bucket=BUCKET)["Contents"]]
    assert names == [f"files/{key[:2]}/{key}"]


async def test_s3_multipart_for_objects_over_the_default_part_size(moto_s3: Any) -> None:
    storage = make_s3()  # default 8 MiB parts
    key = new_key()
    data = os.urandom(MIB) * 17  # 17 MiB: parts of 8 + 8 + 1 MiB

    async def stream() -> AsyncIterator[bytes]:
        for i in range(0, len(data), 3 * MIB):
            yield data[i : i + 3 * MIB]
            assert not await storage.exists(key)  # nothing is visible while uploading

    assert await storage.put(key, stream()) == len(data)
    assert await storage.size(key) == len(data)
    assert await read_all(storage, key, chunk_size=MIB) == data
    assert moto_s3.list_multipart_uploads(Bucket=BUCKET).get("Uploads", []) == []


async def test_s3_multipart_uses_several_parts(
    moto_s3: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = make_s3(part_size=5 * MIB)
    calls = {"parts": 0}
    original = storage.client.upload_part

    def counting(**kwargs: Any) -> Any:
        calls["parts"] += 1
        return original(**kwargs)

    monkeypatch.setattr(storage.client, "upload_part", counting)
    key = new_key()
    await storage.put(key, chunks(b"a" * (11 * MIB)))
    assert calls["parts"] == 3
    assert await storage.size(key) == 11 * MIB


async def test_s3_abort_after_a_successful_multipart_commit_makes_no_s3_call(
    moto_s3: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = make_s3(part_size=5 * MIB)
    aborts: list[Any] = []
    monkeypatch.setattr(storage.client, "abort_multipart_upload", lambda **kw: aborts.append(kw))
    writer = await storage.open_writer(new_key())
    await writer.write(b"a" * (6 * MIB))
    await writer.commit()
    await writer.abort()
    assert aborts == []


async def test_s3_failed_multipart_is_aborted(moto_s3: Any) -> None:
    storage = make_s3(part_size=5 * MIB)
    key = new_key()

    async def broken() -> AsyncIterator[bytes]:
        yield b"a" * (6 * MIB)  # starts a multipart upload
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await storage.put(key, broken())
    assert not await storage.exists(key)
    assert moto_s3.list_multipart_uploads(Bucket=BUCKET).get("Uploads", []) == []
    assert moto_s3.list_objects_v2(Bucket=BUCKET).get("Contents", []) == []


async def test_s3_failed_commit_leaves_nothing(
    moto_s3: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = make_s3(part_size=5 * MIB)
    key = new_key()

    def boom(**_: Any) -> Any:
        raise RuntimeError("complete failed")

    monkeypatch.setattr(storage.client, "complete_multipart_upload", boom)
    with pytest.raises(RuntimeError):
        await storage.put(key, chunks(b"a" * (6 * MIB)))
    assert not await storage.exists(key)
    assert moto_s3.list_multipart_uploads(Bucket=BUCKET).get("Uploads", []) == []


async def test_s3_check_fails_for_missing_bucket(moto_s3: Any) -> None:
    storage = make_s3(bucket="does-not-exist")
    with pytest.raises(StorageError):
        await storage.check()


async def test_s3_errors_do_not_leak_details(moto_s3: Any) -> None:
    storage = make_s3(bucket="does-not-exist")
    with pytest.raises(StorageError) as info:
        await storage.put(new_key(), chunks(b"x"))
    assert "does-not-exist" not in str(info.value)


async def test_s3_sweep_aborts_only_stale_uploads_under_our_prefix(moto_s3: Any) -> None:
    storage = make_s3()
    ours = moto_s3.create_multipart_upload(Bucket=BUCKET, Key=f"files/ab/{'ab' * 16}")
    other = moto_s3.create_multipart_upload(Bucket=BUCKET, Key="other/thing")
    # moto stamps every upload with 2010-11-10, so "not stale" means a limit of decades
    assert await storage.sweep_stale_temp(40 * 365 * 86400) == 0
    assert await storage.sweep_stale_temp(3600) == 1  # only our prefix is touched
    remaining = {u["UploadId"] for u in moto_s3.list_multipart_uploads(Bucket=BUCKET)["Uploads"]}
    assert remaining == {other["UploadId"]}
    assert ours["UploadId"] not in remaining


def test_s3_client_uses_r2_compatible_settings() -> None:
    storage = make_s3()
    config = storage.client.meta.config
    assert config.request_checksum_calculation == "when_required"
    assert config.response_checksum_validation == "when_required"
    assert config.s3["addressing_style"] == "path"
