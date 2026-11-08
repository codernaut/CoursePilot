"""S3Storage: objects in an S3-compatible bucket (AWS S3, Cloudflare R2, MinIO, ...).

Layout: ``files/<first two key chars>/<key>`` inside the bucket (a fixed prefix, so the bucket can
be shared and the stale-upload sweep only touches our own multipart uploads). Only keys matching
``^[0-9a-f]{32}$`` are accepted, exactly like ``LocalStorage``.

Writes are atomic by construction: a small object is a single ``PutObject``, a large one a
multipart upload that only becomes an object on ``CompleteMultipartUpload``. ``abort`` aborts the
multipart upload, so a failed write leaves nothing under the key. Uploads that were interrupted by
a crash are cleaned up by ``sweep_stale_temp`` (the worker calls it on every tick).

boto3 is blocking, so every call runs in a thread (``asyncio.to_thread``); the event loop is never
blocked. The client is created on first use. Checksums are ``when_required`` because boto3 >= 1.36
otherwise adds CRC32 trailers that some S3-compatible services reject.
"""

import asyncio
import contextlib
import threading
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import Settings
from app.storage.base import (
    READ_CHUNK_SIZE,
    InvalidKeyError,
    Storage,
    StorageError,
    StorageNotFoundError,
    StorageWriter,
)
from app.storage.local import KEY_PATTERN

PREFIX = "files/"
PART_SIZE = 8 * 1024 * 1024  # S3 requires >= 5 MiB for every part except the last
MAX_PARTS = 10_000
_NOT_FOUND_CODES = {"404", "NoSuchKey", "NotFound"}


def _code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def _wrap(exc: Exception, action: str) -> StorageError:
    if isinstance(exc, ClientError):
        return StorageError(f"S3 {action} failed: {_code(exc) or 'error'}")
    return StorageError(f"S3 {action} failed: {type(exc).__name__}")


def object_name(key: str) -> str:
    """The bucket object name for ``key``; ``InvalidKeyError`` for anything but a real key."""
    if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key):
        raise InvalidKeyError("invalid storage key")
    return f"{PREFIX}{key[:2]}/{key}"


class _S3Writer(StorageWriter):
    def __init__(self, storage: "S3Storage", key: str, name: str, part_size: int) -> None:
        self._storage = storage
        self._key = key
        self._name = name
        self._part_size = part_size
        self._buffer = bytearray()
        self._upload_id: str | None = None
        self._parts: list[dict[str, Any]] = []
        self._done = False

    def _client(self) -> Any:
        return self._storage.client

    def _upload_part(self, data: bytes) -> None:
        client = self._client()
        if self._upload_id is None:
            self._upload_id = client.create_multipart_upload(
                Bucket=self._storage.bucket, Key=self._name
            )["UploadId"]
        number = len(self._parts) + 1
        if number > MAX_PARTS:
            raise StorageError("object is too large for a multipart upload")
        response = client.upload_part(
            Bucket=self._storage.bucket,
            Key=self._name,
            UploadId=self._upload_id,
            PartNumber=number,
            Body=data,
        )
        self._parts.append({"ETag": response["ETag"], "PartNumber": number})

    def _write(self, data: bytes) -> None:
        if self._done:
            raise StorageError("writer is closed")
        self._buffer += data
        while len(self._buffer) >= self._part_size:
            part = bytes(self._buffer[: self._part_size])
            del self._buffer[: self._part_size]
            self._upload_part(part)

    def _commit(self) -> None:
        if self._done:
            raise StorageError("writer is closed")
        client = self._client()
        if self._storage.object_exists(self._name):
            raise StorageError("storage key is already in use")
        if self._upload_id is None:
            client.put_object(Bucket=self._storage.bucket, Key=self._name, Body=bytes(self._buffer))
        else:
            if self._buffer:
                self._upload_part(bytes(self._buffer))
            client.complete_multipart_upload(
                Bucket=self._storage.bucket,
                Key=self._name,
                UploadId=self._upload_id,
                MultipartUpload={"Parts": self._parts},
            )
        self._buffer.clear()
        self._upload_id = None  # completed: a later abort() must not touch S3
        self._done = True

    def _abort(self) -> None:
        self._buffer.clear()
        upload_id, self._upload_id = self._upload_id, None
        self._done = True
        if upload_id is not None:
            # on failure the stale-upload sweep removes it later
            with contextlib.suppress(ClientError, BotoCoreError):
                self._client().abort_multipart_upload(
                    Bucket=self._storage.bucket, Key=self._name, UploadId=upload_id
                )

    async def write(self, data: bytes) -> None:
        try:
            await asyncio.to_thread(self._write, data)
        except (ClientError, BotoCoreError) as exc:
            raise _wrap(exc, "write") from exc

    async def commit(self) -> None:
        try:
            await asyncio.to_thread(self._commit)
        except BaseException as exc:
            await self.abort()
            if isinstance(exc, ClientError | BotoCoreError):
                raise _wrap(exc, "commit") from exc
            raise

    async def abort(self) -> None:
        if self._done and self._upload_id is None:
            return
        await asyncio.to_thread(self._abort)


class S3Storage(Storage):
    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None = None,
        region: str = "auto",
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        part_size: int = PART_SIZE,
        client: Any = None,
    ) -> None:
        self.bucket = bucket
        self._endpoint_url = endpoint_url
        self._region = region
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._part_size = part_size
        self._client: Any = client
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> "S3Storage":
        access = settings.s3_access_key_id
        secret = settings.s3_secret_access_key
        return cls(
            bucket=settings.s3_bucket,
            endpoint_url=settings.s3_endpoint_url,
            region=settings.s3_region,
            access_key_id=access.get_secret_value() if access else None,
            secret_access_key=secret.get_secret_value() if secret else None,
        )

    @property
    def client(self) -> Any:
        """The boto3 client, created on first use (thread-safe; boto3 clients are thread-safe)."""
        with self._lock:
            if self._client is None:
                import boto3

                self._client = boto3.client(
                    "s3",
                    endpoint_url=self._endpoint_url,
                    region_name=self._region,
                    aws_access_key_id=self._access_key_id,
                    aws_secret_access_key=self._secret_access_key,
                    config=Config(
                        signature_version="s3v4",
                        s3={"addressing_style": "path"},
                        request_checksum_calculation="when_required",
                        response_checksum_validation="when_required",
                        retries={"max_attempts": 5, "mode": "standard"},
                        connect_timeout=10,
                        read_timeout=60,
                    ),
                )
            return self._client

    def object_exists(self, name: str) -> bool:
        """Blocking existence check (call from a thread)."""
        try:
            self.client.head_object(Bucket=self.bucket, Key=name)
        except ClientError as exc:
            if _code(exc) in _NOT_FOUND_CODES:
                return False
            raise
        return True

    async def open_writer(self, key: str) -> StorageWriter:
        return _S3Writer(self, key, object_name(key), self._part_size)

    async def read(self, key: str, chunk_size: int = READ_CHUNK_SIZE) -> AsyncIterator[bytes]:
        name = object_name(key)
        try:
            response = await asyncio.to_thread(self.client.get_object, Bucket=self.bucket, Key=name)
        except ClientError as exc:
            if _code(exc) in _NOT_FOUND_CODES:
                raise StorageNotFoundError("object not found") from None
            raise _wrap(exc, "read") from exc
        except BotoCoreError as exc:
            raise _wrap(exc, "read") from exc
        body = response["Body"]
        iterator = body.iter_chunks(chunk_size)
        try:
            while True:
                try:
                    chunk = await asyncio.to_thread(next, iterator, None)
                except (ClientError, BotoCoreError) as exc:
                    raise _wrap(exc, "read") from exc
                if chunk is None:
                    break
                if chunk:
                    yield chunk
        finally:
            await asyncio.to_thread(body.close)

    async def delete(self, key: str) -> None:
        name = object_name(key)
        try:
            await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=name)
        except (ClientError, BotoCoreError) as exc:
            raise _wrap(exc, "delete") from exc

    async def exists(self, key: str) -> bool:
        name = object_name(key)
        try:
            return await asyncio.to_thread(self.object_exists, name)
        except (ClientError, BotoCoreError) as exc:
            raise _wrap(exc, "exists") from exc

    async def size(self, key: str) -> int:
        name = object_name(key)
        try:
            head = await asyncio.to_thread(self.client.head_object, Bucket=self.bucket, Key=name)
        except ClientError as exc:
            if _code(exc) in _NOT_FOUND_CODES:
                raise StorageNotFoundError("object not found") from None
            raise _wrap(exc, "size") from exc
        except BotoCoreError as exc:
            raise _wrap(exc, "size") from exc
        return int(head["ContentLength"])

    def _check(self) -> None:
        self.client.head_bucket(Bucket=self.bucket)
        self.client.list_objects_v2(Bucket=self.bucket, Prefix=PREFIX, MaxKeys=1)

    async def check(self) -> None:
        """Prove the bucket is reachable with our credentials (readiness probe)."""
        try:
            await asyncio.to_thread(self._check)
        except (ClientError, BotoCoreError) as exc:
            raise _wrap(exc, "check") from exc

    def _sweep(self, max_age_s: float) -> int:
        cutoff = datetime.now(UTC) - timedelta(seconds=max_age_s)
        removed = 0
        marker: dict[str, str] = {}
        while True:
            page = self.client.list_multipart_uploads(Bucket=self.bucket, Prefix=PREFIX, **marker)
            for upload in page.get("Uploads", []):
                initiated = upload["Initiated"]
                if initiated.tzinfo is None:
                    initiated = initiated.replace(tzinfo=UTC)
                if initiated < cutoff:
                    try:
                        self.client.abort_multipart_upload(
                            Bucket=self.bucket, Key=upload["Key"], UploadId=upload["UploadId"]
                        )
                        removed += 1
                    except ClientError:
                        continue
            if not page.get("IsTruncated"):
                return removed
            marker = {
                "KeyMarker": page["NextKeyMarker"],
                "UploadIdMarker": page["NextUploadIdMarker"],
            }

    async def sweep_stale_temp(self, max_age_s: float) -> int:
        """Abort multipart uploads older than ``max_age_s`` under our prefix."""
        try:
            return await asyncio.to_thread(self._sweep, max_age_s)
        except (ClientError, BotoCoreError) as exc:
            raise _wrap(exc, "sweep") from exc
