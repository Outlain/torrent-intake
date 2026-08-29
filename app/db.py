import os
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .config import get_settings

settings = get_settings()

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
if settings.database_url.startswith("sqlite"):
    # The job database can contain private magnet parameters. Keep SQLite's
    # database, WAL, and shared-memory files private even if the host directory's
    # group is shared with other media services.
    os.umask(0o077)
engine = create_engine(settings.database_url, connect_args=connect_args, future=True)


def _secure_sqlite_files(database_url: str) -> None:
    database = make_url(database_url).database
    if not database or database == ":memory:":
        return
    path = Path(database)
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            candidate.chmod(0o600)
        except FileNotFoundError:
            continue


if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record) -> None:
        _secure_sqlite_files(settings.database_url)
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
        _secure_sqlite_files(settings.database_url)


SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)


class Base(DeclarativeBase):
    pass


SCHEMA_ADDITIONS: dict[str, dict[str, str]] = {
    "jobs": {
        "quarantine_path": "TEXT",
        "custom_tags_json": "TEXT NOT NULL DEFAULT '[]'",
    },
    "scan_runs": {
        "current_file_started_at": "DATETIME",
        "engine_version": "VARCHAR(128)",
        "database_version": "VARCHAR(64)",
        "database_updated_at": "DATETIME",
        "policy_version": "VARCHAR(128)",
    },
    "scan_files": {
        "ctime_ns": "INTEGER",
        "device": "INTEGER",
        "inode": "INTEGER",
        "engine_version": "VARCHAR(128)",
        "database_version": "VARCHAR(64)",
        "database_updated_at": "DATETIME",
        "policy_version": "VARCHAR(128)",
        "scan_method": "VARCHAR(64)",
        "scan_started_at": "DATETIME",
        "scan_duration_seconds": "FLOAT",
    },
    "scanner_control": {
        "maintenance_mode": "BOOLEAN NOT NULL DEFAULT 0",
        "maintenance_reason": "TEXT",
        "maintenance_started_at": "DATETIME",
    },
}

SCHEMA_INDEXES: dict[str, tuple[str, tuple[str, ...]]] = {
    "ix_scan_files_job_status_scanned": (
        "scan_files",
        ("job_id", "status", "scanned_at"),
    ),
}


def upgrade_schema(bind: Engine = engine) -> None:
    """Apply additive migrations for installations created before migrations existed."""
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())
    existing_indexes = {
        table_name: {index["name"] for index in inspector.get_indexes(table_name)}
        for table_name in existing_tables
    }
    with bind.begin() as connection:
        for table_name, additions in SCHEMA_ADDITIONS.items():
            if table_name not in existing_tables:
                continue
            existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
            for column_name, definition in additions.items():
                if column_name in existing_columns:
                    continue
                connection.execute(
                    text(f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {definition}')
                )
        for index_name, (table_name, columns) in SCHEMA_INDEXES.items():
            if table_name not in existing_tables:
                continue
            if index_name in existing_indexes.get(table_name, set()):
                continue
            column_sql = ", ".join(f'"{column}"' for column in columns)
            connection.execute(
                text(
                    f'CREATE INDEX IF NOT EXISTS "{index_name}" '
                    f'ON "{table_name}" ({column_sql})'
                )
            )


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
