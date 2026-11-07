"""Cheap zip sanity checks that run BEFORE ``zipfile`` loads a central directory.

``zipfile.ZipFile`` reads the whole central directory into memory and builds one ``ZipInfo`` per
entry. An archive that declares millions of entries (or a gigantic directory) can therefore exhaust
memory before any later check on ``infolist()`` runs. The end-of-central-directory (EOCD) record
sits in the last bytes of the file and states the entry count and directory size, so we read just
that record and refuse hostile values first. Used by the upload-time check and by the parsers.
"""

import struct
from typing import IO

MAX_ZIP_ENTRIES = 10_000
# A directory entry is 46 bytes plus name/extra/comment; 10,000 entries with long names stay far
# below this, so a bigger declaration is hostile.
MAX_CENTRAL_DIRECTORY_BYTES = 8 * 1024 * 1024

_EOCD_SIGNATURE = b"PK\x05\x06"
_EOCD_STRUCT = struct.Struct("<4s4H2LH")  # 22 bytes
_ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
_ZIP64_LOCATOR_STRUCT = struct.Struct("<4sLQL")  # 20 bytes
_ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"
_ZIP64_EOCD_STRUCT = struct.Struct("<4sQ2H2L4Q")  # 56 bytes
_MAX_COMMENT = 0xFFFF


class ZipGuardError(ValueError):
    """The archive is not acceptable; the message is safe to show to users."""


def precheck_zip_directory(
    file: IO[bytes],
    *,
    max_entries: int = MAX_ZIP_ENTRIES,
    max_directory_bytes: int = MAX_CENTRAL_DIRECTORY_BYTES,
) -> None:
    """Validate the EOCD record (and the ZIP64 one when present) without parsing the directory.

    ``file`` must be seekable. Raises ``ZipGuardError``; returns normally when the declared entry
    count and directory size are acceptable and the directory lies inside the file.
    """
    file.seek(0, 2)
    size = file.tell()
    if size < _EOCD_STRUCT.size:
        raise ZipGuardError("is not a valid zip archive")
    tail_len = min(size, _EOCD_STRUCT.size + _MAX_COMMENT)
    file.seek(size - tail_len)
    tail = file.read(tail_len)
    pos = tail.rfind(_EOCD_SIGNATURE)
    if pos < 0 or len(tail) - pos < _EOCD_STRUCT.size:
        raise ZipGuardError("is not a valid zip archive")
    (_sig, _disk, _cd_disk, _entries_here, entries, cd_size, cd_offset, _comment) = (
        _EOCD_STRUCT.unpack_from(tail, pos)
    )
    eocd_offset = size - tail_len + pos

    if entries == 0xFFFF or 0xFFFFFFFF in (cd_size, cd_offset):
        entries, cd_size, cd_offset = _read_zip64(file, eocd_offset, entries, cd_size, cd_offset)

    if entries > max_entries:
        raise ZipGuardError("contains too many parts")
    if cd_size > max_directory_bytes:
        raise ZipGuardError("has an oversized directory")
    if cd_offset + cd_size > size:
        raise ZipGuardError("is truncated or corrupt")


def _read_zip64(
    file: IO[bytes], eocd_offset: int, entries: int, cd_size: int, cd_offset: int
) -> tuple[int, int, int]:
    locator_at = eocd_offset - _ZIP64_LOCATOR_STRUCT.size
    if locator_at < 0:
        # No room for a locator: the 0xFFFF values are literal (a plain, huge declaration).
        return entries, cd_size, cd_offset
    file.seek(locator_at)
    raw = file.read(_ZIP64_LOCATOR_STRUCT.size)
    if len(raw) < _ZIP64_LOCATOR_STRUCT.size:
        return entries, cd_size, cd_offset
    sig, _disk, record_offset, _total_disks = _ZIP64_LOCATOR_STRUCT.unpack(raw)
    if sig != _ZIP64_LOCATOR_SIGNATURE:
        return entries, cd_size, cd_offset
    file.seek(record_offset)
    record = file.read(_ZIP64_EOCD_STRUCT.size)
    if len(record) < _ZIP64_EOCD_STRUCT.size:
        raise ZipGuardError("is truncated or corrupt")
    values = _ZIP64_EOCD_STRUCT.unpack(record)
    if values[0] != _ZIP64_EOCD_SIGNATURE:
        raise ZipGuardError("is truncated or corrupt")
    # (sig, size, ver_made, ver_needed, disk, cd_disk, entries_here, entries, cd_size, cd_offset)
    return int(values[7]), int(values[8]), int(values[9])
