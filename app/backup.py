"""Bounded, encrypted SQLite/settings snapshots. No torrent content is included."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import time
import zipfile

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from sqlalchemy.engine import make_url

from .config import Settings
from .state_files import read_private, write_json, write_private


MAGIC = b"TI-BACKUP-1\0"
CHUNK = 1024 * 1024
MAX_DATABASE_BYTES = 512 * CHUNK
MAX_BACKUP_BYTES = MAX_DATABASE_BYTES + 4 * CHUNK
MEMBERS = {"manifest.json", "settings.json", "torrent_intake.db", "deployment-notes.txt"}


def database_path(settings: Settings) -> Path:
    url = make_url(settings.database_url)
    if url.drivername not in {"sqlite", "sqlite+pysqlite"} or not url.database or url.query:
        raise ValueError("Portable backups require a file-based SQLite database inside TI_DATA_DIR")
    path = Path(url.database)
    root = Path(settings.data_dir).resolve()
    if not path.is_absolute() or path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("The SQLite database must be a regular file inside TI_DATA_DIR")
    if path.name in {"settings.json", "admin-token", "deployment-notes.txt", "controller.lock",
                     "controller-paused.json", "restart-required", "last-restore.json"}:
        raise ValueError("Database filename conflicts with an application settings file")
    if path.exists() and not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("The SQLite database is not a regular file")
    return path


def database_size_bytes(settings: Settings) -> int:
    """Logical SQLite size including committed WAL pages, without a table scan."""
    source = database_path(settings)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=3)) as connection:
        connection.execute("BEGIN")
        pages = connection.execute("PRAGMA page_count").fetchone()[0]
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        return pages * page_size


def _key(passphrase: str, salt: bytes) -> bytes:
    encoded = passphrase.encode("utf-8")
    if len(passphrase) < 12 or len(encoded) > 1024:
        raise ValueError("Use a backup passphrase of at least 12 characters and at most 1024 UTF-8 bytes")
    return Scrypt(salt=salt, length=32, n=2**17, r=8, p=1).derive(encoded)


def encrypt_archive(source: Path, destination: Path, passphrase: str) -> None:
    if source.stat().st_size > MAX_BACKUP_BYTES:
        raise ValueError("Backup exceeds the 512 MiB database budget")
    salt, nonce = os.urandom(16), os.urandom(12)
    header = MAGIC + salt + nonce
    encryptor = Cipher(algorithms.AES(_key(passphrase, salt)), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(header)
    with source.open("rb") as incoming, destination.open("xb") as outgoing:
        os.chmod(destination, 0o600)
        outgoing.write(header)
        while chunk := incoming.read(CHUNK):
            outgoing.write(encryptor.update(chunk))
        outgoing.write(encryptor.finalize())
        outgoing.write(encryptor.tag)
        outgoing.flush()
        os.fsync(outgoing.fileno())


def decrypt_archive(source: Path, destination: Path, passphrase: str) -> None:
    length = source.stat().st_size
    header_size = len(MAGIC) + 28
    if not header_size + 16 < length <= MAX_BACKUP_BYTES + header_size + 16:
        raise ValueError("Invalid or oversized encrypted backup")
    try:
        with source.open("rb") as incoming, destination.open("xb") as outgoing:
            os.chmod(destination, 0o600)
            header = incoming.read(header_size)
            if not header.startswith(MAGIC):
                raise ValueError("Unsupported backup format")
            salt, nonce = header[len(MAGIC):len(MAGIC) + 16], header[-12:]
            incoming.seek(-16, os.SEEK_END)
            tag = incoming.read(16)
            incoming.seek(header_size)
            decryptor = Cipher(algorithms.AES(_key(passphrase, salt)), modes.GCM(nonce, tag)).decryptor()
            decryptor.authenticate_additional_data(header)
            remaining = length - header_size - 16
            while remaining:
                chunk = incoming.read(min(CHUNK, remaining))
                if not chunk:
                    raise ValueError("Truncated encrypted backup")
                outgoing.write(decryptor.update(chunk))
                remaining -= len(chunk)
            # Nothing may inspect this plaintext archive before authentication.
            outgoing.write(decryptor.finalize())
            outgoing.flush()
            os.fsync(outgoing.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def check_database(path: Path) -> None:
    if path.stat().st_size > MAX_DATABASE_BYTES:
        raise ValueError("SQLite backup exceeds 512 MiB; use an offline database backup instead")
    deadline = time.monotonic() + 60
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)) as connection:
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite backup failed its integrity check")
        schema = connection.execute("SELECT type,name,sql FROM sqlite_master").fetchall()
        tables = {name for kind, name, _ in schema if kind == "table" and not name.startswith("sqlite_")}
        if tables != {"jobs", "scan_runs", "scan_files", "scanner_control"}:
            raise ValueError("Backup does not contain the expected Torrent Intake database")
        if any(kind not in {"table", "index"} or "VIRTUAL TABLE" in (sql or "").upper() for kind, _, sql in schema):
            raise ValueError("Backup contains unsupported database objects")
        for table, required in {
            "jobs": {"id", "state", "qbt_hash", "unique_tag", "magnet_uri"},
            "scan_runs": {"job_id", "root_path", "worker_id"},
            "scan_files": {"job_id", "relative_path", "status", "size_bytes"},
            "scanner_control": {"id", "requested_slots"},
        }.items():
            columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            if not required <= columns:
                raise ValueError(f"Backup has an incompatible {table} table")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("Backup has inconsistent database relationships")


def snapshot_database(source: Path, destination: Path) -> None:
    deadline = time.monotonic() + 60

    def progress(status, remaining, total):
        if time.monotonic() >= deadline or destination.stat().st_size > MAX_DATABASE_BYTES:
            raise ValueError("SQLite snapshot exceeded its time or size budget")

    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=3)) as incoming:
        with closing(sqlite3.connect(destination)) as outgoing:
            os.chmod(destination, 0o600)
            incoming.backup(outgoing, pages=256, progress=progress, sleep=0.05)
    check_database(destination)
    with destination.open("rb") as handle:
        os.fsync(handle.fileno())


def create_backup(settings: Settings, directory: Path, passphrase: str) -> Path:
    source = database_path(settings)
    if source.stat().st_size > MAX_DATABASE_BYTES:
        raise ValueError("SQLite database exceeds the portable backup size limit")
    estimated_size = source.stat().st_size
    journal = Path(str(source) + "-wal")
    if journal.is_symlink():
        raise ValueError("Refusing a symbolic-link SQLite journal")
    if journal.exists():
        estimated_size += journal.stat().st_size
    if shutil.disk_usage(directory).free < estimated_size * 3 + 32 * CHUNK:
        raise ValueError("Not enough free space on the application data volume for a backup")
    snapshot_database(source, directory / "torrent_intake.db")
    write_json(directory / "settings.json", {"schema_version": 1, "settings": settings.model_dump(mode="json")})
    try:
        notes = read_private(Path(settings.data_dir) / "deployment-notes.txt")
    except FileNotFoundError:
        notes = b"Save your actual Portainer/Compose stack here before migrating. Host mounts and sidecar configuration are not visible to the app.\n"
    write_private(directory / "deployment-notes.txt", notes)
    write_json(directory / "manifest.json", {
        "application": "torrent-intake", "backup_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "includes_secrets": True,
        "excludes": ["torrent files", "qBittorrent state", "ClamAV definitions", "notifier state and events", "Docker images"],
    })
    archive = directory / "snapshot.zip"
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_STORED) as handle:
        for name in sorted(MEMBERS):
            handle.write(directory / name, arcname=name)
    output = directory / "torrent-intake.tibak"
    encrypt_archive(archive, output, passphrase)
    return output


def unpack_backup(archive: Path, directory: Path) -> dict:
    with zipfile.ZipFile(archive) as handle:
        entries = handle.infolist()
        if len(entries) != len(MEMBERS) or {item.filename for item in entries} != MEMBERS:
            raise ValueError("Backup contains missing, duplicate, or unexpected paths")
        for item in entries:
            maximum = MAX_DATABASE_BYTES if item.filename == "torrent_intake.db" else CHUNK
            kind = stat.S_IFMT(item.external_attr >> 16)
            if item.compress_type != zipfile.ZIP_STORED or item.flag_bits & 1 or kind not in {0, stat.S_IFREG}:
                raise ValueError("Backup must contain only uncompressed regular files")
            if not 0 <= item.file_size <= maximum or item.compress_size != item.file_size:
                raise ValueError("Backup member exceeds its size budget")
            # Fixed allowlisted member names; never use ZipFile.extract/all.
            with handle.open(item) as incoming, (directory / item.filename).open("xb") as outgoing:
                os.chmod(directory / item.filename, 0o600)
                copied = 0
                while chunk := incoming.read(CHUNK):
                    copied += len(chunk)
                    if copied > maximum:
                        raise ValueError("Backup member exceeded its declared size")
                    outgoing.write(chunk)
                outgoing.flush()
                os.fsync(outgoing.fileno())
    manifest = json.loads(read_private(directory / "manifest.json"))
    if not isinstance(manifest, dict) or manifest.get("application") != "torrent-intake" or manifest.get("backup_version") != 1:
        raise ValueError("Unsupported Torrent Intake backup version")
    payload = json.loads(read_private(directory / "settings.json"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1 or not isinstance(payload.get("settings"), dict):
        raise ValueError("Unsupported settings format in backup")
    if set(payload["settings"]) - set(Settings.model_fields):
        raise ValueError("Backup requires a newer Torrent Intake version")
    check_database(directory / "torrent_intake.db")
    return payload["settings"]


def file_digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()
