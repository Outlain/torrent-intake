from __future__ import annotations

import asyncio
from collections import namedtuple
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from cryptography.exceptions import InvalidTag
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import Response

from app.admin import Controller
from app.backup import create_backup, database_path, database_size_bytes, decrypt_archive, encrypt_archive, check_database, unpack_backup
from app.config import Settings, persist_settings
from app.db import Base
from app.models import Job, ScanRun, ScanFile
from app.restore import apply_pending_restore, stage_restore
from app.settings_view import build_settings_catalog, ui_editable
from app.state_files import read_private, write_json, write_private


PASSPHRASE = "a long test-only backup phrase"


def make_database(root: Path, identifier: str = "original") -> Settings:
    settings = Settings(data_dir=str(root), database_url=f"sqlite:///{root / 'torrent_intake.db'}",
                        qbt_password="private-qbt-password", ui_title="Saved title")
    engine = create_engine(settings.database_url)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Job(id=identifier, magnet_uri="magnet:?private-passkey=test-private-key", final_parent="/downloads/Movies",
                   staging_preference="nas", staging_root_initial="/downloads/torrent-intake/staging",
                   managed_tag="torrent_intake", unique_tag=f"ti_job_{identifier}"))
        db.commit()
        db.add(ScanRun(job_id=identifier, root_path="/downloads/torrent-intake/staging"))
        db.add(ScanFile(job_id=identifier, relative_path="movie.mkv", size_bytes=123, mtime_ns=456, status="clean"))
        db.commit()
    engine.dispose()
    return settings


def job_ids(path: Path) -> list[str]:
    with closing(sqlite3.connect(path)) as db:
        return [row[0] for row in db.execute("SELECT id FROM jobs ORDER BY id")]


class SettingsPersistenceTests(unittest.TestCase):
    def test_environment_wins_and_is_saved_for_later_without_environment(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TI_DATA_DIR": directory}):
            path = Path(directory) / "settings.json"
            write_json(path, {"schema_version": 1, "settings": {"local_max_gib": 100, "qbt_password": "saved-secret"}})
            with patch.dict(os.environ, {"TI_LOCAL_MAX_GIB": "250", "TI_QBT_PASSWORD": "environment-secret"}):
                settings = Settings(_env_file=None)
                self.assertEqual(settings.local_max_gib, 250)
                self.assertEqual(settings.qbt_password, "environment-secret")
                persist_settings(settings)
                catalog = repr(build_settings_catalog(settings))
                self.assertNotIn("environment-secret", catalog)
                self.assertIn("Environment override", catalog)
            restored = Settings(_env_file=None)
            self.assertEqual(restored.local_max_gib, 250)
            self.assertEqual(restored.qbt_password, "environment-secret")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_legacy_dotenv_overrides_saved_file(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TI_DATA_DIR": directory}):
            path = Path(directory)
            write_json(path / "settings.json", {"schema_version": 1, "settings": {"local_max_gib": 100}})
            (path / "legacy.env").write_text("TI_LOCAL_MAX_GIB=300\n")
            self.assertEqual(Settings(_env_file=path / "legacy.env").local_max_gib, 300)

    def test_unknown_or_malformed_file_fails_instead_of_silently_using_defaults(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TI_DATA_DIR": directory}):
            path = Path(directory) / "settings.json"
            for value in ({"settings": {}}, {"schema_version": 1, "settings": {"typo_setting": 1}}):
                write_json(path, value)
                with self.assertRaises(ValueError):
                    Settings(_env_file=None)

    def test_bootstrap_location_cannot_be_changed_by_saved_file(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TI_DATA_DIR": directory}):
            write_json(Path(directory) / "settings.json", {"schema_version": 1, "settings": {"data_dir": "/untrusted"}})
            self.assertEqual(Settings(_env_file=None).data_dir, directory)

    def test_safety_critical_and_bootstrap_settings_are_not_ui_editable(self):
        for name in ("app_name", "infected_action", "local_staging_root", "final_parent_prefix", "database_url", "data_dir", "ffmpeg_binary", "unknown"):
            self.assertFalse(ui_editable(name), name)
        self.assertTrue(ui_editable("qbt_password"))
        self.assertTrue(ui_editable("polling_interval_seconds"))

    def test_private_files_reject_symlinks_and_oversized_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source").write_bytes(b"private")
            (root / "alias").symlink_to(root / "source")
            with self.assertRaises(OSError):
                read_private(root / "alias")
            with self.assertRaises(ValueError):
                write_private(root / "alias", b"changed")
            with self.assertRaises(ValueError):
                read_private(root / "source", 2)
            self.assertEqual((root / "source").read_bytes(), b"private")


class BackupRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {"TI_DATA_DIR": str(self.root)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.settings = make_database(self.root)
        self.work = self.root / "export"
        self.work.mkdir()

    def backup(self):
        return create_backup(self.settings, self.work, PASSPHRASE)

    def test_encrypted_snapshot_includes_secrets_checkpoints_and_committed_wal(self):
        source = database_path(self.settings)
        with closing(sqlite3.connect(source)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("UPDATE jobs SET torrent_name='WAL-only update'")
            writer.commit()
            backup = self.backup()
        self.assertNotIn(b"private-qbt-password", backup.read_bytes())
        self.assertNotIn(b"test-private-key", backup.read_bytes())
        archive = self.root / "verified.zip"
        decrypt_archive(backup, archive, PASSPHRASE)
        restored = self.root / "unpacked"
        restored.mkdir()
        values = unpack_backup(archive, restored)
        self.assertEqual(values["qbt_password"], "private-qbt-password")
        self.assertNotIn("admin-token", {item.name for item in restored.iterdir()})
        with closing(sqlite3.connect(restored / "torrent_intake.db")) as db:
            self.assertEqual(db.execute("SELECT torrent_name FROM jobs").fetchone()[0], "WAL-only update")
            self.assertEqual(db.execute("SELECT status FROM scan_files").fetchone()[0], "clean")

    def test_wrong_passphrase_and_modified_ciphertext_leave_no_plaintext(self):
        backup = self.backup()
        target = self.root / "decrypted.zip"
        with self.assertRaises(InvalidTag):
            decrypt_archive(backup, target, "a different wrong passphrase")
        self.assertFalse(target.exists())
        data = bytearray(backup.read_bytes())
        data[-30] ^= 1
        damaged = self.root / "damaged.tibak"
        damaged.write_bytes(data)
        with self.assertRaises(InvalidTag):
            decrypt_archive(damaged, target, PASSPHRASE)
        self.assertFalse(target.exists())
        self.assertEqual(job_ids(database_path(self.settings)), ["original"])

    def test_weak_passphrase_rejected(self):
        plain = self.root / "plain"
        plain.write_bytes(b"test")
        with self.assertRaises(ValueError):
            encrypt_archive(plain, self.root / "encrypted", "short")

    def test_unknown_archive_paths_and_compression_are_rejected(self):
        self.backup()
        for index, extra in enumerate(("../escape", "/absolute", "unexpected")):
            archive = self.root / f"bad-{index}.zip"
            with zipfile.ZipFile(self.work / "snapshot.zip") as original, zipfile.ZipFile(archive, "w") as bad:
                for item in original.infolist():
                    bad.writestr(item.filename, original.read(item))
                bad.writestr(extra, b"bad")
            target = self.root / f"unpack-{index}"
            target.mkdir()
            with self.assertRaises(ValueError):
                unpack_backup(archive, target)
            self.assertEqual(list(target.iterdir()), [])
        archive = self.root / "compressed.zip"
        with zipfile.ZipFile(self.work / "snapshot.zip") as original, zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bad:
            for item in original.infolist():
                bad.writestr(item.filename, original.read(item))
        with self.assertRaises(ValueError):
            unpack_backup(archive, self.root)

    def test_database_triggers_are_rejected(self):
        path = database_path(self.settings)
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TRIGGER unexpected AFTER DELETE ON jobs BEGIN SELECT 1; END")
            db.commit()
        with self.assertRaisesRegex(ValueError, "unsupported database objects"):
            check_database(path)

    def test_database_and_ciphertext_size_limits_are_checked(self):
        with patch("app.backup.MAX_DATABASE_BYTES", 16), self.assertRaises(ValueError):
            check_database(database_path(self.settings))
        backup = self.backup()
        target = self.root / "decrypted.zip"
        with patch("app.backup.MAX_BACKUP_BYTES", 16), self.assertRaises(ValueError):
            decrypt_archive(backup, target, PASSPHRASE)
        self.assertFalse(target.exists())

    def test_restore_checks_space_for_existing_rollback_database(self):
        backup = self.backup()
        directory = self.root / "upload"
        directory.mkdir()
        usage = namedtuple("Usage", "total used free")
        with patch("app.restore.shutil.disk_usage", return_value=usage(1024, 1024, 0)), self.assertRaisesRegex(ValueError, "free space"):
            stage_restore(backup, directory, self.settings, PASSPHRASE)
        self.assertFalse((self.root / ".restore-pending").exists())
        self.assertEqual(job_ids(database_path(self.settings)), ["original"])

    def test_backup_checks_space_for_uncheckpointed_wal(self):
        source = database_path(self.settings)
        with closing(sqlite3.connect(source)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("UPDATE jobs SET torrent_name='WAL-only update'")
            writer.commit()
            self.assertGreater(Path(str(source) + "-wal").stat().st_size, 0)
            free = source.stat().st_size * 3 + 32 * 1024 * 1024
            usage = namedtuple("Usage", "total used free")
            with patch("app.backup.shutil.disk_usage", return_value=usage(free, 0, free)), self.assertRaisesRegex(ValueError, "free space"):
                self.backup()
        self.assertEqual(list(self.work.iterdir()), [])

    def test_database_size_includes_wal_without_imposing_backup_limit(self):
        source = database_path(self.settings)
        with closing(sqlite3.connect(source)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("UPDATE jobs SET torrent_name=?", ("x" * 100000,))
            writer.commit()
            logical_size = database_size_bytes(self.settings)
            self.assertGreater(logical_size, source.stat().st_size)
            with patch("app.backup.MAX_DATABASE_BYTES", 16):
                self.assertEqual(database_size_bytes(self.settings), logical_size)
        self.assertEqual(job_ids(source), ["original"])

    def test_restore_is_staged_then_rebased_and_applied_offline_with_rollback(self):
        backup = self.backup()
        destination = self.root / "new-machine"
        destination.mkdir()
        current = make_database(destination, "previous-installation")
        with patch.dict(os.environ, {"TI_DATA_DIR": str(destination)}):
            persist_settings(current)
            upload_work = destination / "upload"
            upload_work.mkdir()
            stage_restore(backup, upload_work, current, PASSPHRASE)
            self.assertEqual(job_ids(database_path(current)), ["previous-installation"])
            apply_pending_restore(current)
            self.assertEqual(job_ids(database_path(current)), ["original"])
            self.assertTrue((destination / "controller-paused.json").exists())
            self.assertFalse((destination / ".restore-pending").exists())
            restored = Settings(_env_file=None)
            self.assertEqual(restored.database_url, current.database_url)
            self.assertEqual(restored.data_dir, str(destination))
            rollback = json.loads(read_private(destination / "last-restore.json"))["rollback_directory"]
            self.assertEqual(job_ids(Path(rollback) / "torrent_intake.db"), ["previous-installation"])
            self.assertEqual(stat.S_IMODE(database_path(current).stat().st_mode), 0o600)

    def test_restart_between_database_and_settings_replacements_recovers(self):
        from app import restore
        backup = self.backup()
        with closing(sqlite3.connect(database_path(self.settings))) as db:
            db.execute("UPDATE jobs SET torrent_name='before restore'")
            db.commit()
        persist_settings(self.settings)
        directory = self.root / "upload"
        directory.mkdir()
        stage_restore(backup, directory, self.settings, PASSPHRASE)
        original = restore._copy_atomic
        calls = 0

        def fail_once(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated power loss")
            return original(source, target)

        with patch("app.restore._copy_atomic", side_effect=fail_once), self.assertRaises(OSError):
            apply_pending_restore(self.settings)
        self.assertTrue((self.root / ".restore-pending").exists())
        apply_pending_restore(self.settings)
        rollback = Path(json.loads(read_private(self.root / "last-restore.json"))["rollback_directory"])
        with closing(sqlite3.connect(rollback / "torrent_intake.db")) as db:
            self.assertEqual(db.execute("SELECT torrent_name FROM jobs").fetchone()[0], "before restore")


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_token_persists_and_is_local_to_the_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(data_dir=str(root))
            first = Controller(settings, fresh=True)
            restarted = Controller(settings, fresh=False)
            self.assertEqual(first.token, restarted.token)
            self.assertEqual(stat.S_IMODE((root / "admin-token").stat().st_mode), 0o600)
            replacement = root / "replacement"
            replacement.mkdir()
            self.assertNotEqual(Controller(Settings(data_dir=str(replacement)), fresh=True).token, first.token)

    async def test_pause_cancels_workers_and_waits_for_mutating_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(Settings(data_dir=directory), fresh=True)
            self.assertTrue(controller.status()["drained"])
            self.assertFalse(controller.authorized("wrong"))
            self.assertTrue(controller.authorized(controller.token))

            async def worker(stop, scanner_stop):
                await stop.wait()
                self.assertTrue(scanner_stop.is_set())

            with patch("app.admin.worker_loop", side_effect=worker):
                controller.resume()
                controller.active_mutations = 1
                controller.pause()
                await controller.shutdown()
                self.assertFalse(controller.status()["drained"])
                controller.active_mutations = 0
                self.assertTrue(controller.status()["drained"])
                write_private(Path(directory) / "restart-required", b"pending")
                with self.assertRaises(ValueError):
                    controller.resume()

    async def test_admin_authentication_and_pause_guard_precede_request_handling(self):
        from app import main
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(Settings(data_dir=directory), fresh=True)
            called = []

            async def handler(request):
                called.append(request.url.path)
                return Response("ok")

            def request(path, *, method="POST", headers=()):
                return Request({"type": "http", "method": method, "scheme": "http", "path": path,
                                "query_string": b"", "headers": [(b"host", b"localhost"), *headers]})

            with patch("app.main.controller", controller):
                self.assertEqual((await main.administration_guard(request("/admin/backup"), handler)).status_code, 403)
                self.assertEqual((await main.administration_guard(request("/jobs"), handler)).status_code, 503)
                self.assertEqual(called, [])
                headers = [(b"x-ti-admin-token", controller.token.encode()), (b"origin", b"http://other-host")]
                self.assertEqual((await main.administration_guard(request("/admin/backup", headers=headers), handler)).status_code, 403)
                headers = [(b"x-ti-admin-token", controller.token.encode())]
                self.assertEqual((await main.administration_guard(request("/admin/status", method="GET", headers=headers), handler)).status_code, 200)
                self.assertEqual(controller.active_mutations, 0)
