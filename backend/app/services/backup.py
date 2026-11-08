"""Backups and restores of the SQLite files and (for LocalStorage) the uploaded files.

The databases are copied with SQLite's online backup API (``sqlite3.Connection.backup``), which is
consistent with WAL mode and safe while the web app and the worker are running. Uploaded files are
immutable once stored (random keys, written atomically), so a plain copy is consistent too; the
temporary ``.tmp`` folder is skipped. With ``STORAGE_BACKEND=s3`` the bucket is not copied: use the
provider's own versioning/replication for it.

A backup is a folder ``coursepilot-backup-<UTC timestamp>`` with the database copies, ``files/``
and ``manifest.json`` (version, commit, sizes and SHA-256 of every file). ``.env`` is NOT included
because it holds secrets; keep a copy of it in a password manager or secret store.
"""

import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app import __version__
from app.core.config import Settings
from app.core.version import get_commit

MANIFEST_NAME = "manifest.json"
MANIFEST_FORMAT = 1
_SQLITE_PREFIX = "sqlite+aiosqlite:///"
TMP_DIRNAME = ".tmp"


class BackupError(Exception):
    """A backup or restore could not be completed; the message says why."""


@dataclass(frozen=True)
class BackupResult:
    path: Path
    manifest: dict[str, Any]


def database_path(settings: Settings) -> Path:
    if not settings.database_url.startswith(_SQLITE_PREFIX):
        raise BackupError(
            "Only SQLite databases (DATABASE_URL=sqlite+aiosqlite:///...) are supported."
        )
    raw = settings.database_url[len(_SQLITE_PREFIX) :]
    if not raw or raw.startswith(":memory:"):
        raise BackupError("An in-memory database cannot be backed up.")
    return Path(raw)


def default_backup_dir(settings: Settings) -> Path:
    return Path(settings.data_dir) / "backups"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _entry(root: Path, path: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "size": path.stat().st_size,
        "sha256": _sha256(path),
    }


def backup_sqlite(source: Path, destination: Path) -> None:
    """Copy a live SQLite database consistently and verify the copy."""
    if not source.is_file():
        raise BackupError(f"Database file not found: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(source, timeout=30)
    try:
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
            result = dst.execute("PRAGMA integrity_check").fetchone()
            if result is None or result[0] != "ok":
                raise BackupError(f"Integrity check of the backup of {source.name} failed.")
        finally:
            dst.close()
    finally:
        src.close()


def _copy_local_files(settings: Settings, destination: Path) -> int:
    source = Path(settings.data_dir) / "files"
    count = 0
    if not source.is_dir():
        return 0
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if TMP_DIRNAME in relative.parts or not path.is_file() or path.is_symlink():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        count += 1
    return count


def create_backup(settings: Settings, out_dir: Path | None = None) -> BackupResult:
    """Write a timestamped backup folder under ``out_dir`` (default ``DATA_DIR/backups``)."""
    base = out_dir or default_backup_dir(settings)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    folder = base / f"coursepilot-backup-{stamp}"
    if folder.exists():
        raise BackupError(f"Backup folder already exists: {folder}")
    folder.mkdir(parents=True)
    try:
        backup_sqlite(database_path(settings), folder / "coursepilot.db")
        checkpoint = Path(settings.checkpoint_db_path)
        if checkpoint.is_file():
            backup_sqlite(checkpoint, folder / "checkpoints.db")
        files_copied = 0
        if settings.storage_backend == "local":
            files_copied = _copy_local_files(settings, folder / "files")
        entries = [_entry(folder, p) for p in sorted(folder.rglob("*")) if p.is_file()]
        manifest: dict[str, Any] = {
            "format": MANIFEST_FORMAT,
            "version": __version__,
            "commit": get_commit(),
            "created_at": datetime.now(UTC).isoformat(),
            "storage_backend": settings.storage_backend,
            "files_included": settings.storage_backend == "local",
            "file_count": files_copied,
            "total_bytes": sum(int(e["size"]) for e in entries),
            "entries": entries,
        }
        (folder / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return BackupResult(folder, manifest)


def read_manifest(folder: Path) -> dict[str, Any]:
    path = folder / MANIFEST_NAME
    if not path.is_file():
        raise BackupError(f"Not a CoursePilot backup (no {MANIFEST_NAME}): {folder}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise BackupError(f"{MANIFEST_NAME} is not valid JSON.") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != MANIFEST_FORMAT:
        raise BackupError("Unsupported backup format.")
    return manifest


def verify_backup(folder: Path) -> dict[str, Any]:
    """Check every file against the manifest; returns the manifest or raises ``BackupError``.

    The manifest must list ``coursepilot.db``, and every file in the folder must be listed (so
    nothing unverified is ever restored). Symlinks are refused.
    """
    manifest = read_manifest(folder)
    root = folder.resolve()
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise BackupError("The manifest has no list of entries.")
    listed: set[str] = set()
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("path"), str)
            or not isinstance(entry.get("sha256"), str)
        ):
            raise BackupError("The manifest has a malformed entry.")
        name = entry["path"]
        path = (root / name).resolve()
        if not path.is_relative_to(root) or (root / name).is_symlink():
            raise BackupError(f"Unsafe path in manifest: {name}")
        if not path.is_file():
            raise BackupError(f"Missing file in backup: {name}")
        if _sha256(path) != entry["sha256"]:
            raise BackupError(f"Checksum mismatch: {name}")
        listed.add(path.relative_to(root).as_posix())
    if "coursepilot.db" not in listed:
        raise BackupError("The backup does not contain coursepilot.db.")
    for path in folder.rglob("*"):
        if path.is_symlink():
            raise BackupError(f"Unsafe symbolic link in backup: {path.name}")
        if path.is_file():
            rel = path.relative_to(folder).as_posix()
            if rel != MANIFEST_NAME and rel not in listed:
                raise BackupError(f"File in the backup that is not listed in the manifest: {rel}")
    return manifest


def restore_backup(settings: Settings, folder: Path) -> list[str]:
    """Replace the live databases (and local files) with a verified backup.

    The app and the worker MUST be stopped. Existing files are not deleted: they are renamed to
    ``<name>.pre-restore-<timestamp>`` so a mistaken restore can be undone. Returns what was done.
    """
    manifest = verify_backup(folder)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    done: list[str] = []

    def move_aside(path: Path) -> None:
        if path.exists():
            aside = path.with_name(f"{path.name}.pre-restore-{stamp}")
            path.rename(aside)
            done.append(f"kept the old {path.name} as {aside.name}")
        for suffix in ("-wal", "-shm"):
            leftover = path.with_name(path.name + suffix)
            leftover.unlink(missing_ok=True)

    main_db = database_path(settings)
    move_aside(main_db)
    main_db.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(folder / "coursepilot.db", main_db)
    done.append(f"restored {main_db.name}")

    if (folder / "checkpoints.db").is_file():
        checkpoint = Path(settings.checkpoint_db_path)
        move_aside(checkpoint)
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(folder / "checkpoints.db", checkpoint)
        done.append(f"restored {checkpoint.name}")

    if manifest.get("files_included") and settings.storage_backend == "local":
        files = Path(settings.data_dir) / "files"
        move_aside(files)
        source = folder / "files"
        if source.is_dir():
            shutil.copytree(source, files)
        else:
            files.mkdir(parents=True)
        done.append(f"restored {manifest['file_count']} uploaded file(s)")
    elif settings.storage_backend == "s3":
        done.append("S3 storage was not touched (the bucket is not part of the backup)")
    return done
