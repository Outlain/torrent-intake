from __future__ import annotations

from dataclasses import replace
import binascii
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from app.archive_tools import ArchiveError, ArchiveLimits, archive_format, cleanup_archive_workspaces, scan_archive
from app.config import Settings
from app.paths import canonical_final_parent
from app.scan_coordinator import ScanCoordinator
from app.scanner import ScannerIdentity, ScannerLimitError, ScannerPolicyError, ScannerService, ScanInterrupted
from archive_fixtures import rar4, rar5, zip_archive


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.source = self.root / "torrent" / "Albums" / "Photos.rar"
        self.source.parent.mkdir(parents=True)
        self.limits = ArchiveLimits(16 * 1024**2, 100, 4, 0, 10)
        self.seen = []

    def inspect(self, path, name):
        self.assertTrue(Path(path).is_relative_to(self.data / "archive-scan"))
        self.seen.append((name, Path(path).read_bytes()))
        self.assertFalse(Path(path).stat().st_mode & 0o111)
        return False, None, "clamd_native"

    def scan(self, payload, *, inspect=None, active=lambda: None, limits=None, **storage):
        self.source.write_bytes(payload)
        with self.source.open("rb") as source:
            return scan_archive(source.fileno(), data_dir=str(self.data), limits=limits or self.limits,
                                check_active=active, scan_member=inspect or self.inspect, **storage)

    def assert_cleaned(self):
        self.assertEqual([p.name for p in (self.data / "archive-scan").iterdir()], ["workspace.lock"])

    def test_custom_scratch_keeps_copies_and_free_space_checks_off_data_disk(self):
        storage = self.root / "nas-scratch"
        storage.mkdir()
        marker = storage / ".mounted"
        marker.touch()
        unrelated = storage / "keep.txt"
        unrelated.write_text("other storage contents")
        def inspect(path, name):
            self.assertTrue(Path(path).is_relative_to(storage / "archive-scan"))
            self.assertEqual(Path(path).read_bytes(), b"photo")
            # Another controller using the same mount cannot clean this active scan.
            cleanup_archive_workspaces(str(self.root / "other-data"), str(storage), str(marker))
            self.assertTrue(Path(path).is_file())
            return False, None, "clamd_native"
        result = self.scan(rar5([("Album/photo.jpg", b"photo")]), inspect=inspect,
                           scratch_dir=str(storage), mount_marker=str(marker))
        self.assertFalse(result[0])
        self.assertFalse(self.data.exists())
        self.assertEqual(unrelated.read_text(), "other storage contents")
        self.assertEqual([p.name for p in (storage / "archive-scan").iterdir()], ["workspace.lock"])
        with self.assertRaisesRegex(ArchiveError, "scratch space"):
            self.scan(rar5([("a", b"b")]), scratch_dir=str(storage),
                      limits=replace(self.limits, reserve_bytes=2**62))

    def test_missing_custom_storage_or_marker_never_falls_back_locally(self):
        storage = self.root / "not-mounted"
        with self.assertRaisesRegex(ArchiveError, "scratch storage"):
            self.scan(rar5([("a", b"b")]), scratch_dir=str(storage))
        self.assertFalse(storage.exists())
        storage.mkdir()
        with self.assertRaisesRegex(ArchiveError, "mount marker"):
            self.scan(rar5([("a", b"b")]), scratch_dir=str(storage), mount_marker=str(storage / ".mounted"))
        self.assertFalse((storage / "archive-scan").exists())
        self.assertFalse(self.data.exists())

    def test_custom_storage_rejects_symlinked_workspaces_and_markers(self):
        storage = self.root / "scratch"
        storage.mkdir()
        (storage / "archive-scan").symlink_to(self.source.parent, target_is_directory=True)
        with self.assertRaises(ArchiveError):
            self.scan(rar5([("a", b"b")]), scratch_dir=str(storage))
        (storage / "archive-scan").unlink()
        target = storage / "marker-target"
        target.touch()
        (storage / ".mounted").symlink_to(target)
        with self.assertRaisesRegex(ArchiveError, "mount marker"):
            self.scan(rar5([("a", b"b")]), scratch_dir=str(storage), mount_marker=str(storage / ".mounted"))
        self.assertFalse((storage / "archive-scan").exists())

    def test_custom_startup_cleanup_is_scoped_to_its_private_subdirectory(self):
        storage = self.root / "scratch"
        workspace = storage / "archive-scan"
        workspace.mkdir(parents=True, mode=0o700)
        old = workspace / "scan-abcdefgh"
        old.mkdir()
        (old / "member-000001").touch()
        unrelated = storage / "scan-abcdefgh"
        unrelated.mkdir()
        cleanup_archive_workspaces(str(self.data), str(storage))
        self.assertFalse(old.exists())
        self.assertTrue(unrelated.exists())
        self.assertFalse(self.data.exists())

    def test_real_rar4_rar5_and_zip_members_are_scanned_without_changing_torrent(self):
        members = [("Holiday/one.jpg", b"one"), ("Holiday/Nested/two.jpg", b"two")]
        for make in (rar4, rar5, zip_archive):
            with self.subTest(format=make.__name__):
                self.seen.clear()
                payload = make(members)
                result = self.scan(payload)
                self.assertFalse(result[0])
                self.assertEqual(self.seen, members)
                self.assertEqual(self.source.read_bytes(), payload)
                self.assertEqual(sorted(p.relative_to(self.root / "torrent").as_posix()
                                        for p in (self.root / "torrent").rglob("*")),
                                 ["Albums", "Albums/Photos.rar"])
                self.assert_cleaned()

    def test_magic_not_extension_selects_archive_format(self):
        for maker, expected in ((rar4, "rar"), (rar5, "rar5"), (zip_archive, "zip")):
            self.source.write_bytes(maker([("a", b"b")]))
            with self.source.open("rb") as source:
                self.assertEqual(archive_format(source.fileno()), expected)

    def test_compressed_and_solid_rar5_fixtures(self):
        root = Path(__file__).parent / "fixtures" / "libarchive"
        for name in ("compressed", "solid", "encrypted", "encrypted_filenames"):
            lines = (root / f"test_read_format_rar5_{name}.rar.uu").read_bytes().splitlines()
            payload = b"".join(binascii.a2b_uu(line) for line in lines[1:] if line not in (b"end", b"`", b" "))
            with self.subTest(name=name):
                self.seen.clear()
                if "encrypted" in name:
                    with self.assertRaisesRegex(ArchiveError, "encrypted"):
                        self.scan(payload)
                else:
                    result = self.scan(payload)
                    self.assertFalse(result[0])
                    self.assertTrue(self.seen)
                    self.assertTrue(all(content for _, content in self.seen))
                self.assert_cleaned()

    def test_nested_archives_share_a_budget_and_scan_leaves(self):
        payload = rar5([("nested.zip", zip_archive([("sub/a.txt", b"hello")]))])
        self.scan(payload)
        self.assertEqual(self.seen, [("nested.zip!/sub/a.txt", b"hello")])
        self.assert_cleaned()

    def test_detected_member_stops_scan_and_cleans_scratch(self):
        def infected(path, name):
            self.inspect(path, name)
            return True, "Test.Signature", "clamd_native"
        result = self.scan(rar4([("a", b"first"), ("b", b"second")]), inspect=infected)
        self.assertTrue(result[0])
        self.assertEqual(result[1], "Test.Signature")
        self.assertEqual(self.seen, [("a", b"first")])
        self.assert_cleaned()

    def test_member_scan_error_is_never_clean(self):
        def failed(path, name):
            raise ScannerPolicyError("scan limit")
        with self.assertRaisesRegex(ScannerPolicyError, "scan limit"):
            self.scan(rar5([("a", b"hello")]), inspect=failed)
        self.assert_cleaned()

    def test_unsafe_paths_are_rejected_without_writing_outside_scratch(self):
        for name in ("../escape", "/absolute", "C:\\escape", "folder\\..\\escape", "bad\nname"):
            with self.subTest(name=name), self.assertRaisesRegex(ArchiveError, "unsafe archive member path"):
                self.scan(zip_archive([(name, b"content")]))
            self.assert_cleaned()

    def test_symlinks_are_rejected(self):
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaisesRegex(ArchiveError, "links or special"):
            self.scan(zip_archive([(info, b"../../escape")]))
        self.assert_cleaned()

    def test_directories_are_allowed_but_only_regular_members_are_scanned(self):
        self.scan(zip_archive([("Photos/", b""), ("Photos/a.jpg", b"photo")]))
        self.assertEqual(self.seen, [("Photos/a.jpg", b"photo")])

    def test_truncated_and_corrupt_members_fail_closed(self):
        for make in (rar4, rar5, zip_archive):
            for corrupted in (False, True):
                payload = make([("photo.jpg", b"unique-image-payload")])
                if corrupted:
                    # Stored RAR payloads and uncompressed ZIPs permit direct corruption.
                    if make is zip_archive:
                        payload = zip_archive([("photo.jpg", b"unique-image-payload")], compression=zipfile.ZIP_STORED)
                    payload = payload.replace(b"unique-image-payload", b"broken-image-payload")
                else:
                    payload = payload[:len(payload)//2]
                with self.subTest(format=make.__name__, corrupted=corrupted), self.assertRaises(ArchiveError):
                    self.scan(payload)
                self.assert_cleaned()

    def test_multipart_and_encrypted_rar_headers_stay_held(self):
        for payload in (rar4([], flags=1), rar5([], flags=1), rar4([], flags=0x80)):
            with self.assertRaisesRegex(ArchiveError, "multipart|encrypted"):
                self.scan(payload)
            self.assert_cleaned()

    def test_missing_rar_end_markers_and_trailing_bytes_stay_held(self):
        for make in (rar4, rar5):
            original = make([("a", b"b")])
            for payload in (original[:-7], original + b"unaccounted payload"):
                with self.subTest(format=make.__name__), self.assertRaises(ArchiveError):
                    self.scan(payload)
                self.assert_cleaned()

    def test_expansion_file_count_depth_and_disk_reserve_limits(self):
        examples = [
            (zip_archive([("big", b"x" * 10000)]), replace(self.limits, expanded_bytes=20), "expanded-byte"),
            (zip_archive([("a", b"x"), ("b", b"y")]), replace(self.limits, files=1), "entry count"),
            (zip_archive([("nested.rar", rar5([("a", b"x")]))]), replace(self.limits, depth=1), "nesting"),
            (zip_archive([("a", b"x")]), replace(self.limits, reserve_bytes=2**62), "scratch space"),
        ]
        for payload, limits, message in examples:
            with self.subTest(message=message), self.assertRaisesRegex(ArchiveError, message):
                self.scan(payload, limits=limits)
            self.assert_cleaned()

    def test_nested_expansion_cannot_reset_total_byte_limit(self):
        nested = zip_archive([("a", b"x" * 1000)])
        with self.assertRaisesRegex(ArchiveError, "expanded-byte"):
            self.scan(rar5([("nested.zip", nested)]), limits=replace(self.limits, expanded_bytes=len(nested) + 500))
        self.assert_cleaned()

    def test_interruption_terminates_helper_and_removes_files(self):
        def active():
            if self.seen:
                raise ScanInterrupted("paused")
        with self.assertRaisesRegex(ScanInterrupted, "paused"):
            self.scan(rar4([("a", b"hello")]), active=active)
        self.assert_cleaned()

    def test_timeout_includes_member_scan_time(self):
        def slow(path, name):
            time.sleep(1.1)
            return self.inspect(path, name)
        with self.assertRaisesRegex(ArchiveError, "timed out"):
            self.scan(zip_archive([("a", b"b")]), inspect=slow, limits=replace(self.limits, timeout_seconds=1))
        self.assert_cleaned()

    def test_startup_cleanup_removes_only_private_workspaces(self):
        scratch = self.data / "archive-scan"
        scratch.mkdir(parents=True, mode=0o700)
        (scratch / "scan-abcdefgh").mkdir()
        (scratch / "scan-abcdefgh" / "member-000001").write_text("old copy")
        (scratch / "keep").mkdir()
        (scratch / "scan-ijklmnop").symlink_to(self.source.parent, target_is_directory=True)
        cleanup_archive_workspaces(str(self.data))
        self.assertFalse((scratch / "scan-abcdefgh").exists())
        self.assertTrue((scratch / "keep").exists())
        self.assertTrue(self.source.parent.exists())


class ArchiveRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "Photos.rar"
        self.scanner = ScannerService()
        self.scanner.settings = self.scanner.settings.model_copy(update={
            "data_dir": str(self.root / "data"), "scanner_max_file_mib": 1,
            "archive_free_space_buffer_gib": 0,
        })
        self.identity = ScannerIdentity("clamd", "test", "1", None, self.scanner.policy_version(), "test")

    def test_oversized_rar_scans_every_member_and_preserves_source_identity(self):
        members = [("Photos/a.jpg", b"a" * (600 * 1024)), ("Photos/b.jpg", b"b" * (600 * 1024))]
        payload = rar5(members)
        self.source.write_bytes(payload)
        before = self.source.stat()
        seen = []
        def native(descriptor, path, expected, **kwargs):
            seen.append(os.pread(descriptor, expected[2], 0))
            return False, None, "stream: OK"
        with patch.object(self.scanner, "_scan_descriptor", side_effect=native), patch.object(self.scanner, "_probe_large_media_descriptor") as probe:
            result = self.scanner.scan_path(str(self.source), identity=self.identity)
        self.assertTrue(result.clean)
        self.assertEqual(result.scan_method, "archive_members")
        self.assertEqual(seen, [data for _, data in members])
        self.assertIn('"clamd_native": 2', result.raw_output)
        probe.assert_not_called()
        after = self.source.stat()
        self.assertEqual((before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns),
                         (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns))
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).digest(), hashlib.sha256(payload).digest())

    def test_six_gib_rar_routes_to_archive_inspection(self):
        with self.source.open("wb") as source:
            source.write(rar5([("a", b"b")]))
            source.truncate(6 * 1024**3)
        with patch.object(self.scanner, "_scan_archive_descriptor", return_value=(False, None, "tested routing")) as archive, patch.object(self.scanner, "_scan_large_media_descriptor") as media:
            result = self.scanner.scan_path(str(self.source), identity=self.identity)
        archive.assert_called_once()
        media.assert_not_called()
        self.assertEqual(result.scan_method, "archive_members")

    def test_native_expansion_limit_can_use_archive_members(self):
        self.source.write_bytes(rar4([("a", b"b")]))
        with patch.object(self.scanner, "_scan_descriptor", side_effect=[ScannerLimitError("limit", limit_name="maxscansize"), (False, None, "OK")]):
            result = self.scanner.scan_path(str(self.source), identity=self.identity)
        self.assertTrue(result.clean)
        self.assertEqual(result.scan_method, "archive_members")

    def test_native_clean_archive_keeps_native_verdict(self):
        self.source.write_bytes(rar4([("a", b"b")]))
        with patch.object(self.scanner, "_scan_descriptor", return_value=(False, None, "OK")), patch.object(self.scanner, "_scan_archive_descriptor") as archive:
            result = self.scanner.scan_path(str(self.source), identity=self.identity)
        archive.assert_not_called()
        self.assertEqual(result.scan_method, "clamd_native")

    def test_disabled_archive_policy_stays_held(self):
        self.source.write_bytes(rar5([("a", b"x" * (1024**2 + 1))]))
        self.scanner.settings.archive_enabled = False
        with self.assertRaisesRegex(ScannerPolicyError, "archive inspection is disabled"):
            self.scanner.scan_path(str(self.source), identity=self.identity)

    def test_member_limit_or_original_file_change_prevents_clean(self):
        self.source.write_bytes(rar5([("a", b"x" * (600 * 1024)), ("b", b"y" * (600 * 1024))]))
        with patch.object(self.scanner, "_scan_descriptor", side_effect=ScannerPolicyError("cannot fully scan")):
            with self.assertRaisesRegex(ScannerPolicyError, "archive member.*cannot fully scan"):
                self.scanner.scan_path(str(self.source), identity=self.identity)
        def changed(*args, **kwargs):
            os.utime(self.source, ns=(1, 1))
            return False, None, "OK"
        with patch.object(self.scanner, "_scan_descriptor", side_effect=changed), self.assertRaises(RuntimeError):
            self.scanner.scan_path(str(self.source), identity=self.identity)

    def test_archive_policy_limits_invalidate_clean_checkpoints(self):
        before = self.scanner.policy_version()
        self.scanner.settings.archive_max_depth += 1
        self.assertNotEqual(before, self.scanner.policy_version())

    def test_scanner_routes_to_custom_storage_and_holds_if_marker_disappears(self):
        self.source.write_bytes(rar5([("a", b"x" * (600 * 1024)), ("b", b"y" * (600 * 1024))]))
        storage = self.root / "external-scratch"
        storage.mkdir()
        marker = storage / ".mounted"
        marker.touch()
        self.scanner.settings.archive_scratch_dir = str(storage)
        self.scanner.settings.archive_scratch_mount_marker = str(marker)
        def native(descriptor, path, expected, **kwargs):
            self.assertTrue(Path(path).is_relative_to(storage))
            marker.unlink(missing_ok=True)
            return False, None, "OK"
        with patch.object(self.scanner, "_scan_descriptor", side_effect=native):
            with self.assertRaisesRegex(ScannerPolicyError, "mount marker"):
                self.scanner.scan_path(str(self.source), identity=self.identity)
        self.assertFalse((self.root / "data" / "archive-scan").exists())
        self.assertFalse(list((storage / "archive-scan").glob("scan-*")))


class ArchiveStorageSettingsTests(unittest.TestCase):
    def test_default_and_custom_workspace_paths(self):
        self.assertEqual(Settings(data_dir="/local-data").archive_workspace_root, "/local-data/archive-scan")
        configured = Settings(archive_scratch_dir="/archive-scratch", archive_scratch_mount_marker="/archive-scratch/.mounted")
        self.assertEqual(configured.archive_workspace_root, "/archive-scratch/archive-scan")
        self.assertIsNone(Settings(archive_scratch_dir="").archive_scratch_dir)

    def test_invalid_and_overlapping_storage_paths_are_rejected(self):
        for path in ("relative", "/", "/tmp/../data", "/scratch\n", "/staging-local", "/downloads/torrent-intake/staging", "/quarantine"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                Settings(archive_scratch_dir=path)
        for options in (
            {"archive_scratch_mount_marker": "/scratch/.mounted"},
            {"archive_scratch_dir": "/scratch", "archive_scratch_mount_marker": "/elsewhere/marker"},
            {"archive_scratch_dir": "/scratch", "archive_scratch_mount_marker": "/scratch"},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                Settings(**options)

    def test_scratch_workspaces_cannot_be_final_destinations(self):
        settings = Settings(archive_scratch_dir="/downloads/scratch-storage")
        with self.assertRaisesRegex(ValueError, "operational"):
            canonical_final_parent("/downloads/scratch-storage/archive-scan/Movies", settings)
        self.assertEqual(canonical_final_parent("/downloads/Movies", settings), "/downloads/Movies")

    def test_manifest_rejects_scratch_inside_a_legacy_torrent_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Photos.rar").write_bytes(b"original")
            coordinator = ScanCoordinator()
            coordinator.settings = Settings(archive_scratch_dir=str(root))
            with self.assertRaisesRegex(ScannerPolicyError, "outside the torrent"):
                coordinator._filesystem_manifest(root)
            self.assertEqual([p.name for p in root.iterdir()], ["Photos.rar"])


if __name__ == "__main__":
    unittest.main()
