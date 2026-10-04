from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base
from app.models import Job, ScanFile
from app.paths import canonical_final_parent
from app.service import JobService
from app.storage import (StorageUnavailable, ensure_nas_choice, pin_existing_jobs,
                         require_final_storage, require_storage, validate_location_changes)


class NasLocationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.local = self.root / "local"
        self.first, self.second = self.root / "nas-one", self.root / "nas-two"
        for directory in (self.local, self.first / "staging", self.second / "staging"):
            directory.mkdir(parents=True)
        for directory in (self.first, self.second):
            (directory / ".mounted").write_text("mounted")
        self.settings = Settings(
            _env_file=None, local_staging_root=str(self.local), nas_staging_root=str(self.first / "staging"),
            final_parent_prefix=str(self.first), final_parent_prefixes=str(self.second),
            nas_staging_locations=[
                dict(id="one", label="First NAS", path=str(self.first / "staging"), mount_marker=str(self.first / ".mounted")),
                dict(id="two", label="Second NAS", path=str(self.second / "staging"), mount_marker=str(self.second / ".mounted")),
            ], default_nas_staging_id="one",
        )
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.service = JobService.__new__(JobService)
        self.service.settings = self.settings
        self.service.qbt = MagicMock()
        self.service.qbt.find_existing_from_magnet.return_value = None
        self.service.qbt.list_torrents.return_value = []
        self.service.logger = logging.getLogger(__name__)
        self.service.scan_coordinator = MagicMock()
        self.service._resolve_hash_for_job = MagicMock()
        self.service._evaluate_staging_now = MagicMock()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.temporary.cleanup()

    def submit(self, preference="local", nas_id=None):
        return self.service.submit_job(
            self.db, magnet_uri="magnet:?xt=urn:btih:" + "b" * 40,
            final_parent=str(self.second / "Library"), final_category=None,
            staging_preference=preference, nas_staging_id=nas_id,
        )

    def settings_with(self, **updates):
        return Settings(**{**self.settings.model_dump(), **updates})

    def test_legacy_single_root_remains_default(self):
        settings = Settings(_env_file=None, nas_staging_root=str(self.first / "staging"))
        self.assertEqual(settings.default_nas_location.id, "primary")
        self.assertEqual(settings.default_nas_location.path, settings.nas_staging_root)

    def test_multiple_locations_require_one_valid_default(self):
        for value in (None, "missing"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.settings_with(default_nas_staging_id=value)

    def test_invalid_registry_is_rejected(self):
        valid = self.settings.model_dump()["nas_staging_locations"]
        for replacement in (
            [valid[0], valid[0]],
            [valid[0], {**valid[1], "path": valid[0]["path"] + "/nested"}],
            [{**valid[0], "path": str(self.local)}],
            [{**valid[0], "path": "/app/data"}],
            [{**valid[0], "path": "/copy-target/staging"}],
            [{**valid[0], "path": "/downloads/docker/torrent-intake"}],
            [{**valid[0], "path": "/var/lib"}],
            [{**valid[0], "path": "/downloads/../state"}],
            [{**valid[0], "path": "relative"}],
            [{**valid[0], "path": str(self.first)}],
        ):
            with self.subTest(replacement=replacement), self.assertRaises(ValidationError):
                self.settings_with(nas_staging_locations=replacement)

    def test_custom_operational_directories_are_not_staging(self):
        for setting in ("data_dir", "event_dir", "quarantine_root"):
            with self.subTest(setting=setting), self.assertRaises(ValidationError):
                self.settings_with(**{setting: str(self.first / "staging" / "operational")})

    def test_grandfathered_job_does_not_block_unrelated_label_change(self):
        job = self.submit("nas", "one")
        job.nas_staging_id = "legacy-retired"
        job.nas_staging_path = str(self.root / "retired-staging")
        self.db.commit()
        changed = self.settings.model_dump()["nas_staging_locations"]
        changed[1]["label"] = "New label"
        validate_location_changes(self.db, self.settings, self.settings_with(nas_staging_locations=changed))
        self.assertEqual(job.nas_staging_path, str(self.root / "retired-staging"))

    def test_manual_nas_selection_pins_path_and_keeps_final_destination(self):
        job = self.submit("nas", "two")
        self.assertEqual(job.nas_staging_id, "two")
        self.assertEqual(job.nas_mount_marker, str(self.second / ".mounted"))
        self.assertEqual(job.staging_root_actual, str(self.second / "staging"))
        self.assertEqual(job.final_parent, str(self.second / "Library"))
        self.assertEqual(self.service.qbt.add_torrent.call_args.kwargs["save_path"], job.staging_root_actual)

    def test_default_change_does_not_redirect_existing_job_after_restart(self):
        job = self.submit()
        identifier = job.id
        self.db.close()
        self.db = Session(self.engine)
        self.service.settings = self.settings_with(default_nas_staging_id="two")
        old = self.db.get(Job, identifier)
        ensure_nas_choice(old, self.service.settings)
        self.assertEqual(old.nas_staging_id, "one")
        self.assertEqual(self.submit().nas_staging_id, "two")

    def test_unknown_id_does_not_add_or_create_job(self):
        with self.assertRaisesRegex(ValueError, "Unknown NAS"):
            self.submit("nas", "missing")
        self.service.qbt.add_torrent.assert_not_called()
        self.assertEqual(list(self.db.scalars(select(Job))), [])

    def test_unavailable_selected_nas_is_queued_without_switch_or_add(self):
        (self.second / ".mounted").unlink()
        job = self.submit("nas", "two")
        self.assertEqual(job.state, "waiting_for_nas")
        self.assertFalse(job.is_terminal)
        self.assertEqual(job.nas_staging_id, "two")
        self.service.qbt.add_torrent.assert_not_called()
        self.service._process_one(self.db, job, ignore_event_grace=False)
        self.service.qbt.add_torrent.assert_not_called()
        (self.second / ".mounted").write_text("returned")
        self.service._process_one(self.db, job, ignore_event_grace=False)
        self.assertEqual(self.service.qbt.add_torrent.call_args.kwargs["save_path"], str(self.second / "staging"))

    def test_automatic_overflow_uses_pinned_target(self):
        job = self.submit()
        job.qbt_hash = "b" * 40
        job.size_bytes = self.settings.local_max_bytes + 1
        self.service.settings = self.settings_with(default_nas_staging_id="two")
        self.service._apply_local_staging_policy(self.db, job, SimpleNamespace(state="downloading"))
        self.service.qbt.set_save_path.assert_called_once_with(job.qbt_hash, str(self.first / "staging"))
        self.assertEqual(job.staging_actual, "nas")

    def test_missing_overflow_mount_pauses_without_moving(self):
        job = self.submit()
        job.qbt_hash, job.size_bytes = "b" * 40, self.settings.local_max_bytes + 1
        (self.first / ".mounted").unlink()
        self.service._apply_local_staging_policy(self.db, job, SimpleNamespace(state="downloading"))
        self.assertEqual(job.state, "waiting_for_nas")
        self.service.qbt.pause.assert_called_once()
        self.service.qbt.set_save_path.assert_not_called()
        self.service.qbt.resume.assert_not_called()

    def test_paused_nas_overflow_does_not_reserve_future_local_downloads(self):
        blocked = self.submit()
        blocked.qbt_hash, blocked.state = "b" * 40, "waiting_for_nas"
        current = self.submit()
        current.qbt_hash = "c" * 40
        self.db.commit()
        waiting = SimpleNamespace(hash=blocked.qbt_hash, amount_left=10**12, state="pausedDL")
        downloading = SimpleNamespace(hash=current.qbt_hash, amount_left=100, state="downloading")
        self.service.qbt.list_torrents.return_value = [waiting, downloading]
        _, _, reserved, remaining = self.service._local_capacity_snapshot(self.db, current, downloading)
        self.assertEqual((reserved, remaining), (0, 100))
        # Until qB actually confirms the pause, reserve space conservatively.
        waiting.state = "downloading"
        _, _, reserved, _ = self.service._local_capacity_snapshot(self.db, current, downloading)
        self.assertEqual(reserved, 10**12)

    def test_nas_recovery_waits_for_fresh_qbt_snapshot_before_scanning(self):
        job = self.submit("nas", "one")
        job.state, job.qbt_hash = "waiting_for_nas", "b" * 40
        self.service._find_live_torrent_for_job = MagicMock(return_value=SimpleNamespace(state="missingFiles"))
        self.service._ensure_job_can_track_torrent = MagicMock()
        self.service._process_one(self.db, job, ignore_event_grace=False)
        self.assertEqual(job.state, "downloading")
        self.service.qbt.resume.assert_called_once_with(job.qbt_hash)
        self.service.scan_coordinator.queue_job.assert_not_called()

    def test_manual_switch_selects_specific_nas(self):
        job = self.submit()
        job.state, job.qbt_hash = "waiting_for_local_space", "b" * 40
        self.db.commit()
        self.service._local_capacity_snapshot = MagicMock(return_value=(1, 1, 0, 2))
        self.service.process_waiting_for_local_space = MagicMock()
        self.service.move_waiting_job_to_nas(self.db, job_id=job.id, nas_staging_id="two")
        self.service.qbt.set_save_path.assert_called_once_with(job.qbt_hash, str(self.second / "staging"))
        self.assertEqual(job.nas_staging_id, "two")

    def test_new_staging_locations_are_not_valid_final_destinations(self):
        for directory in (self.first, self.second):
            with self.assertRaisesRegex(ValueError, "operational"):
                canonical_final_parent(str(directory / "staging" / "Movie"), self.settings)
        self.assertEqual(canonical_final_parent(str(self.second / "New"), self.settings), str(self.second / "New"))

    def test_copy_output_namespace_cannot_be_used_for_final_promotion(self):
        settings = self.settings_with(final_parent_prefixes="/copy-target")
        with self.assertRaisesRegex(ValueError, "operational"):
            canonical_final_parent("/copy-target/Library", settings)

    def test_category_mount_is_checked_not_its_unmounted_parent(self):
        # Separate category bind mounts can be writable even though their
        # otherwise empty container parent (/downloads) is read-only.
        accessible = {self.second, self.second / ".mounted"}
        with patch("app.storage.os.access", side_effect=lambda path, mode: Path(path) in accessible):
            require_final_storage(self.settings, str(self.second / "NewLibrary"))
        self.assertFalse((self.second / "NewLibrary").exists())

    def test_active_location_cannot_be_removed_or_repointed(self):
        self.submit()
        validate_location_changes(self.db, self.settings, self.settings_with(default_nas_staging_id="two"))
        changed = self.settings.model_dump()["nas_staging_locations"]
        changed[0]["path"] = str(self.first / "other")
        with self.assertRaisesRegex(ValueError, "active job"):
            validate_location_changes(self.db, self.settings, self.settings_with(nas_staging_locations=changed))

    def test_legacy_job_upgrade_keeps_existing_paths_and_checkpoints(self):
        job = self.submit("nas", "two")
        job.nas_staging_path = job.nas_staging_id = None
        self.db.add(ScanFile(job_id=job.id, relative_path="movie", size_bytes=1, mtime_ns=1, status="clean"))
        self.db.commit()
        pin_existing_jobs(self.db, self.settings)
        pin_existing_jobs(self.db, self.settings)
        self.assertEqual(job.nas_staging_path, str(self.second / "staging"))
        self.assertEqual(self.db.scalar(select(ScanFile)).status, "clean")

    def test_readonly_storage_and_symlink_markers_are_rejected(self):
        with patch("app.storage.os.access", return_value=False), self.assertRaises(StorageUnavailable):
            require_storage(str(self.first))
        marker = self.first / "bad-marker"
        marker.symlink_to(self.first / ".mounted")
        with self.assertRaises(StorageUnavailable):
            require_storage(str(self.first), str(marker))

    def test_final_subfolder_can_be_missing_but_nas_marker_cannot(self):
        target = str(self.first / "Library" / "New show")
        require_final_storage(self.settings, target)
        self.assertFalse(Path(target).exists())
        (self.first / ".mounted").unlink()
        with self.assertRaises(StorageUnavailable):
            require_final_storage(self.settings, target)
        # An unrelated offline NAS does not block the second destination.
        require_final_storage(self.settings, str(self.second / "Library"))


if __name__ == "__main__":
    unittest.main()
