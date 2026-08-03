from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

from app.db import SCHEMA_ADDITIONS, SCHEMA_INDEXES, _secure_sqlite_files, upgrade_schema


class AdditiveMigrationTests(unittest.TestCase):
    def test_existing_sqlite_files_are_made_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "private.db"
            candidates = (database, Path(f"{database}-wal"), Path(f"{database}-shm"))
            for candidate in candidates:
                candidate.write_bytes(b"test")
                candidate.chmod(0o644)
            _secure_sqlite_files(f"sqlite+pysqlite:///{database}")
            for candidate in candidates:
                self.assertEqual(stat.S_IMODE(candidate.stat().st_mode), 0o600)

    def test_upgrade_adds_scanner_columns_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = create_engine(
                f"sqlite+pysqlite:///{Path(directory) / 'legacy.db'}",
                future=True,
            )
            with engine.begin() as connection:
                connection.execute(text("CREATE TABLE jobs (id VARCHAR(36) PRIMARY KEY)"))
                connection.execute(text("CREATE TABLE scan_runs (job_id VARCHAR(36) PRIMARY KEY)"))
                connection.execute(
                    text(
                        "CREATE TABLE scan_files ("
                        "id INTEGER PRIMARY KEY, job_id VARCHAR(36), "
                        "status VARCHAR(16), scanned_at DATETIME)"
                    )
                )
                connection.execute(
                    text(
                        "CREATE TABLE scanner_control ("
                        "id INTEGER PRIMARY KEY, requested_slots INTEGER NOT NULL)"
                    )
                )

            upgrade_schema(engine)
            upgrade_schema(engine)

            inspector = inspect(engine)
            for table_name, expected in SCHEMA_ADDITIONS.items():
                columns = {column["name"] for column in inspector.get_columns(table_name)}
                self.assertTrue(set(expected).issubset(columns))
            for index_name, (table_name, _) in SCHEMA_INDEXES.items():
                indexes = {index["name"] for index in inspector.get_indexes(table_name)}
                self.assertIn(index_name, indexes)
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
