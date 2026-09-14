"""Stage a restore while paused; install it only before the application starts."""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from uuid import uuid4

from .backup import CHUNK, MAX_DATABASE_BYTES, check_database, database_path, decrypt_archive, file_digest, snapshot_database, unpack_backup
from .config import Settings
from .state_files import read_private, sync_directory, write_json, write_private


def stage_restore(upload: Path, directory: Path, settings: Settings, passphrase: str) -> dict:
    root = Path(settings.data_dir)
    pending = root / ".restore-pending"
    if pending.exists():
        raise ValueError("A restore is already pending; restart the container first")
    target = database_path(settings)
    previous_size = target.stat().st_size if target.exists() else 0
    if previous_size > MAX_DATABASE_BYTES:
        raise ValueError("The current database needs an offline backup before replacement")
    journal = Path(str(target) + "-wal")
    if journal.exists():
        previous_size += journal.stat().st_size
    if shutil.disk_usage(root).free < upload.stat().st_size * 3 + previous_size + 32 * CHUNK:
        raise ValueError("Not enough local free space to validate and stage this restore")
    archive = directory / "decrypted.zip"
    decrypt_archive(upload, archive, passphrase)
    staged = directory / "validated"
    staged.mkdir(mode=0o700)
    values = unpack_backup(archive, staged)
    # Deployment bootstrap paths belong to the receiving container. Media paths
    # remain unchanged so qBittorrent/file ownership gates still apply.
    values["data_dir"] = settings.data_dir
    values["database_url"] = settings.database_url
    restored = Settings(**values)
    write_json(staged / "settings.json", {"schema_version": 1, "settings": restored.model_dump(mode="json")})
    identifier = uuid4().hex
    manifest = {
        "version": 1, "id": identifier, "database_target": str(target),
        "saved_previous": False,
        "hashes": {name: file_digest(staged / name) for name in ("torrent_intake.db", "settings.json", "deployment-notes.txt")},
    }
    write_json(staged / "apply.json", manifest)
    sync_directory(staged)
    os.rename(staged, pending)
    sync_directory(root)
    return {"restart_required": True, "message": "Restore staged. Restart the container; it will remain paused."}


def _copy_atomic(source: Path, target: Path) -> None:
    if target.is_symlink():
        raise ValueError("Refusing to replace a symlink during restore")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".restore-copy-", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as outgoing, source.open("rb") as incoming:
            shutil.copyfileobj(incoming, outgoing, length=CHUNK)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        os.replace(temporary, target)
        sync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def apply_pending_restore(settings: Settings) -> None:
    root = Path(settings.data_dir)
    pending = root / ".restore-pending"
    if not pending.exists():
        return
    if pending.is_symlink() or not pending.is_dir():
        raise ValueError("Invalid pending restore directory")
    manifest = json.loads(read_private(pending / "apply.json"))
    if not isinstance(manifest, dict) or manifest.get("version") != 1 or not re.fullmatch(r"[0-9a-f]{32}", str(manifest.get("id", ""))):
        raise ValueError("Invalid pending restore manifest")
    target = database_path(settings)
    if manifest.get("database_target") != str(target):
        raise ValueError("Database target changed after upload; refusing to apply the pending restore")
    names = ("torrent_intake.db", "settings.json", "deployment-notes.txt")
    for name in names:
        source = pending / name
        if source.is_symlink() or file_digest(source) != manifest.get("hashes", {}).get(name):
            raise ValueError("Staged restore changed after validation")
    check_database(pending / "torrent_intake.db")
    required_space = (pending / "torrent_intake.db").stat().st_size + 32 * CHUNK
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(target) + suffix)
        if candidate.is_symlink():
            raise ValueError("Refusing a symbolic-link SQLite database/journal")
        if not manifest.get("saved_previous") and candidate.exists():
            required_space += candidate.stat().st_size
    if shutil.disk_usage(root).free < required_space:
        raise ValueError("Not enough local free space to apply the restore and retain rollback data")
    rollback = root / f"before-restore-{manifest['id']}"
    rollback.mkdir(mode=0o700, exist_ok=True)
    if rollback.is_symlink():
        raise ValueError("Invalid rollback directory")
    write_json(root / "controller-paused.json", {"reason": "Restored backup: verify qBittorrent and mounts before resuming"})
    if not manifest.get("saved_previous"):
        if target.exists():
            previous = rollback / "database.snapshot"
            snapshot_database(target, previous)
            os.replace(previous, rollback / "torrent_intake.db")
        for name in ("settings.json", "deployment-notes.txt"):
            try:
                write_private(rollback / name, read_private(root / name))
            except FileNotFoundError:
                pass
        sync_directory(rollback)
        manifest["saved_previous"] = True
        write_json(pending / "apply.json", manifest)

    # The launcher holds the exclusive controller lock and no app engine has
    # been opened. Do not let an old WAL replay over the restored database.
    if target.exists():
        for suffix in ("-wal", "-shm"):
            if Path(str(target) + suffix).is_symlink():
                raise ValueError("Refusing a symbolic-link SQLite journal")
        with closing(sqlite3.connect(target, timeout=3)) as connection:
            if connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
                raise ValueError("Another process is using the database; stop it before restoring")
        for suffix in ("-wal", "-shm"):
            Path(str(target) + suffix).unlink(missing_ok=True)
    _copy_atomic(pending / "torrent_intake.db", target)
    for name in ("settings.json", "deployment-notes.txt"):
        _copy_atomic(pending / name, root / name)
    write_json(root / "last-restore.json", {"rollback_directory": str(rollback), "id": manifest["id"]})
    completed = root / f".restore-complete-{manifest['id']}"
    os.rename(pending, completed)
    sync_directory(root)
    # Only the verified staging copy is removed. The previous installation is
    # retained in before-restore-<id>; the uploaded backup is kept by its owner.
    shutil.rmtree(completed)
