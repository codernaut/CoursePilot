"""The end-of-central-directory pre-check runs before zipfile loads a central directory."""

import io
import zipfile

import pytest

from app.core.zip_guard import ZipGuardError, precheck_zip_directory
from app.services.file_validation import ALLOWED_TYPES, ContentError, check_stored_zip, check_zip
from app.storage import LocalStorage, new_key
from tests.docfiles import add_zip64_eocd, forge_eocd
from tests.files import make_docx, make_pptx

PPTX = ALLOWED_TYPES["pptx"]


def test_a_normal_zip_passes() -> None:
    precheck_zip_directory(io.BytesIO(make_pptx()))


def test_too_many_declared_entries_are_rejected() -> None:
    with pytest.raises(ZipGuardError, match="too many parts"):
        precheck_zip_directory(io.BytesIO(forge_eocd(make_pptx(), entries=10_001)))


def test_an_oversized_directory_is_rejected() -> None:
    with pytest.raises(ZipGuardError, match="oversized directory"):
        precheck_zip_directory(io.BytesIO(forge_eocd(make_pptx(), cd_size=9 * 1024 * 1024)))


def test_a_directory_that_points_past_the_end_is_rejected() -> None:
    data = make_pptx()
    with pytest.raises(ZipGuardError, match="truncated or corrupt"):
        precheck_zip_directory(io.BytesIO(forge_eocd(data, cd_size=len(data) + 1)))


def test_zip64_values_are_read_from_the_zip64_record() -> None:
    zip64 = add_zip64_eocd(make_pptx(), entries=4_000_000, cd_size=64)
    with pytest.raises(ZipGuardError, match="too many parts"):
        precheck_zip_directory(io.BytesIO(zip64))
    huge = add_zip64_eocd(make_pptx(), entries=3, cd_size=2**40)
    with pytest.raises(ZipGuardError, match="oversized directory"):
        precheck_zip_directory(io.BytesIO(huge))


@pytest.mark.parametrize("data", [b"", b"PK\x03\x04", b"x" * 100, b"PK\x05\x06" + b"\x00" * 5])
def test_files_without_a_valid_eocd_are_rejected(data: bytes) -> None:
    with pytest.raises(ZipGuardError, match="not a valid zip"):
        precheck_zip_directory(io.BytesIO(data))


def test_limits_are_adjustable() -> None:
    data = make_docx()
    with pytest.raises(ZipGuardError):
        precheck_zip_directory(io.BytesIO(data), max_entries=1)
    precheck_zip_directory(io.BytesIO(data), max_entries=10)


def test_upload_check_rejects_before_zipfile_opens_the_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    many = forge_eocd(make_pptx(), entries=50_000)
    huge = forge_eocd(make_pptx(), cd_size=2**31)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("zipfile.ZipFile must not be reached for a hostile directory")

    monkeypatch.setattr(zipfile, "ZipFile", forbidden)
    with pytest.raises(ContentError, match="too many parts"):
        check_zip(io.BytesIO(many), PPTX)
    with pytest.raises(ContentError, match="oversized directory"):
        check_zip(io.BytesIO(huge), PPTX)


def test_upload_check_still_accepts_a_valid_pptx() -> None:
    check_zip(io.BytesIO(make_pptx()), PPTX)


async def test_stored_object_check_uses_the_same_guard(tmp_path: object) -> None:
    storage = LocalStorage(str(tmp_path))
    key = new_key()

    async def body() -> object:
        yield forge_eocd(make_pptx(), entries=60_000)

    await storage.put(key, body())  # type: ignore[arg-type]
    with pytest.raises(ContentError, match="too many parts"):
        await check_stored_zip(storage, key, PPTX)
