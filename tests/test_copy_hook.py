import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


from app import copy_action as hook


class CopyHookBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.destination = self.root / "destination"
        self.destination.mkdir()
        (self.destination / hook.MOUNT_MARKER).touch()
        self.source = self.root / "movie.mkv"
        self.source.write_bytes(b"scanned payload")
        self.destination_patch = patch.object(hook, "DESTINATION_ROOT", self.destination)
        self.destination_patch.start()
        self.addCleanup(self.destination_patch.stop)

    def copy(self, source=None, job_id="safe-job"):
        return hook.copy_promoted(source or self.source, "a" * 40, "Movie", job_id)


class CopyHookSafetyTests(CopyHookBase):
    def test_legacy_example_wrapper_uses_shared_engine_and_configured_destination(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "after-promotion-copy.py"
        specification = importlib.util.spec_from_file_location("copy_hook_wrapper", script)
        wrapper = importlib.util.module_from_spec(specification)
        with patch.object(sys, "path", list(sys.path)):
            specification.loader.exec_module(wrapper)
        wrapper.DESTINATION_ROOT = self.destination
        with patch.object(wrapper, "_copy_promoted", return_value=self.destination) as copy:
            self.assertEqual(wrapper.copy_promoted(self.source, "a" * 40, "Movie", "job"), self.destination)
        copy.assert_called_once_with(self.source, "a" * 40, "Movie", "job", destination=self.destination)

    def test_builtin_destination_requires_dedicated_mount_boundary(self):
        with self.assertRaisesRegex(RuntimeError, "/copy-target"):
            hook.validate_destination(self.destination)
        with patch.object(hook, "COPY_ROOT", self.destination):
            hook.validate_destination(self.destination)
            nested = self.destination / "nested"
            nested.mkdir()
            with self.assertRaises(FileNotFoundError):
                hook.validate_destination(nested)
            (nested / hook.MOUNT_MARKER).touch()
            hook.validate_destination(nested)

    def test_builtin_destination_requires_write_permission(self):
        with patch.object(hook, "COPY_ROOT", self.destination), patch.object(hook.os, "access", return_value=False):
            with self.assertRaises(PermissionError):
                hook.validate_destination(self.destination)

    def test_destination_bind_alias_inside_source_is_rejected_before_reservation(self):
        folder = self.root / "series"
        folder.mkdir()
        nested = folder / "destination"
        nested.mkdir()
        (folder / "video.mkv").write_bytes(b"video")
        real_checked_path = hook.checked_path

        def alias_stat(path):
            # Simulate /copy-target binding an existing directory inside the
            # promoted source without privileged mount operations in tests.
            return real_checked_path(nested if path == self.destination else path)

        with patch.object(hook, "checked_path", side_effect=alias_stat), patch.object(hook.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "aliases the source"):
                self.copy(folder)
        run.assert_not_called()
        self.assertFalse((self.destination / "intake-job-safe-job").exists())

    def test_destination_bind_alias_of_source_is_rejected(self):
        folder = self.root / "series"
        folder.mkdir()
        real_checked_path = hook.checked_path
        with patch.object(hook, "checked_path", side_effect=lambda path: real_checked_path(folder if path == self.destination else path)):
            with self.assertRaisesRegex(RuntimeError, "aliases the source"):
                self.copy(folder)
        self.assertFalse((self.destination / "intake-job-safe-job").exists())

    def test_requires_existing_destination_and_operator_mount_marker(self):
        (self.destination / hook.MOUNT_MARKER).unlink()
        with patch.object(hook.subprocess, "run") as run, self.assertRaises(FileNotFoundError):
            self.copy()
        run.assert_not_called()
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_rejects_source_symlink_and_symlink_ancestor(self):
        symlink = self.root / "alias"
        symlink.symlink_to(self.source)
        with self.assertRaisesRegex(RuntimeError, "Symbolic links"):
            self.copy(symlink)
        folder = self.root / "folder"
        folder.mkdir()
        (folder / "movie").write_bytes(b"data")
        alias = self.root / "folder-alias"
        alias.symlink_to(folder, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "Symbolic links"):
            self.copy(alias / "movie")

    def test_rejects_directory_symlinks_and_special_entries(self):
        folder = self.root / "torrent"
        folder.mkdir()
        link = folder / "link"
        link.symlink_to(self.source)
        with self.assertRaisesRegex(RuntimeError, "Symbolic links"):
            self.copy(folder)
        link.unlink()
        os.mkfifo(folder / "fifo")
        with self.assertRaisesRegex(RuntimeError, "Only regular files"):
            self.copy(folder)

    def test_rejects_marker_symlink_and_directory(self):
        marker = self.destination / hook.MOUNT_MARKER
        marker.unlink()
        marker.symlink_to(self.source)
        with self.assertRaisesRegex(RuntimeError, "Symbolic links"):
            self.copy()
        marker.unlink()
        marker.mkdir()
        with self.assertRaisesRegex(RuntimeError, "mount marker"):
            self.copy()

    def test_rejects_identical_and_nested_source_destination(self):
        for source in (self.destination, self.root, self.destination / "nested"):
            if not source.exists():
                source.mkdir()
            with self.subTest(source=source), self.assertRaisesRegex(RuntimeError, "identical or nested"):
                self.copy(source)

    def test_rejects_unsafe_job_ids_and_traversal(self):
        for job_id in ("../escape", "", "a/b", "--bad", "x" * 81):
            with self.subTest(job_id=job_id), self.assertRaisesRegex(RuntimeError, "safe folder"):
                self.copy(job_id=job_id)
        with self.assertRaisesRegex(RuntimeError, "traversal-free"):
            self.copy(self.destination / ".." / self.source.name)

    def test_existing_incomplete_copy_is_not_overwritten_or_resumed(self):
        folder = self.destination / "intake-job-safe-job"
        folder.mkdir()
        partial = folder / self.source.name
        partial.write_bytes(b"partial")
        with patch.object(hook.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "manual retry"):
                self.copy()
        run.assert_not_called()
        self.assertEqual(partial.read_bytes(), b"partial")

    def test_failed_copy_is_left_incomplete_and_source_is_retained(self):
        def fail_copy(argv, **kwargs):
            (Path(argv[-1]) / self.source.name).write_bytes(b"partial")
            raise subprocess.CalledProcessError(23, argv)

        with patch.object(hook.subprocess, "run", side_effect=fail_copy):
            with self.assertRaises(subprocess.CalledProcessError):
                self.copy()
        folder = self.destination / "intake-job-safe-job"
        self.assertFalse((folder / hook.COMPLETE_MARKER).exists())
        self.assertEqual(self.source.read_bytes(), b"scanned payload")
        with self.assertRaisesRegex(RuntimeError, "manual retry"):
            self.copy()

    def test_argument_list_is_local_copy_only_and_failure_does_not_mark_complete(self):
        with patch.object(hook.subprocess, "run", side_effect=RuntimeError("stop")) as run:
            with self.assertRaisesRegex(RuntimeError, "stop"):
                self.copy()
        args = run.call_args.args[0]
        self.assertEqual(args[-3:], ["--", str(self.source), str(self.destination / "intake-job-safe-job") + "/"])
        self.assertIn("--fsync", args)
        for forbidden in ("--delete", "--remove-source-files", "--links", "--copy-links", "-a", "-e"):
            self.assertNotIn(forbidden, args)
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertFalse((self.destination / "intake-job-safe-job" / hook.COMPLETE_MARKER).exists())

    def test_rejects_symlink_destination_or_completion_marker(self):
        alias = self.root / "destination-alias"
        alias.symlink_to(self.destination, target_is_directory=True)
        with patch.object(hook, "DESTINATION_ROOT", alias):
            with self.assertRaisesRegex(RuntimeError, "Symbolic links"):
                self.copy()
        folder = self.destination / "intake-job-safe-job"
        folder.mkdir()
        (folder / hook.COMPLETE_MARKER).symlink_to(self.source)
        with self.assertRaisesRegex(RuntimeError, "manual retry"):
            self.copy()

    def test_malformed_completion_marker_is_not_success(self):
        folder = self.destination / "intake-job-safe-job"
        folder.mkdir()
        marker = folder / hook.COMPLETE_MARKER
        for content in ("", "{", "[]", "null"):
            marker.write_text(content)
            with self.subTest(content=content), self.assertRaisesRegex(RuntimeError, "manual retry"):
                self.copy()


@unittest.skipUnless(shutil.which("rsync"), "rsync is not installed; real-copy integration checks skipped")
class CopyHookRsyncTests(CopyHookBase):
    def setUp(self):
        super().setUp()
        self.rsync_patch = patch.object(hook, "RSYNC", shutil.which("rsync"))
        self.rsync_patch.start()
        self.addCleanup(self.rsync_patch.stop)

    def test_file_copy_and_completed_retry_are_idempotent(self):
        folder = self.copy()
        self.assertEqual((folder / self.source.name).read_bytes(), self.source.read_bytes())
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        record = json.loads((folder / hook.COMPLETE_MARKER).read_text())
        self.assertEqual(record["request"]["job_id"], "safe-job")
        with patch.object(hook.subprocess, "run") as run:
            self.assertEqual(self.copy(), folder)
        run.assert_not_called()
        self.assertTrue(self.source.exists())

    def test_copies_only_selected_folder_including_empty_subdirectories(self):
        folder = self.root / "-movie folder"
        folder.mkdir()
        (folder / "disc").mkdir()
        (folder / "empty").mkdir()
        (folder / "disc" / "track.mkv").write_bytes(b"video")
        output = self.copy(folder)
        self.assertEqual((output / folder.name / "disc" / "track.mkv").read_bytes(), b"video")
        self.assertTrue((output / folder.name / "empty").is_dir())
        self.assertFalse((output / self.source.name).exists())

    def test_completed_copy_collision_or_mutation_is_not_overwritten(self):
        folder = self.copy()
        target = folder / self.source.name
        target.write_bytes(b"modified payload")
        with patch.object(hook.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "no longer matches"):
                self.copy()
        run.assert_not_called()
        self.assertEqual(target.read_bytes(), b"modified payload")

    def test_copy_can_retry_after_operator_moves_incomplete_reservation_aside(self):
        folder = self.destination / "intake-job-safe-job"
        folder.mkdir()
        (folder / self.source.name).write_bytes(b"partial")
        folder.rename(self.destination / "operator-held-partial")
        self.assertEqual(self.copy(), folder)
        self.assertEqual((folder / self.source.name).read_bytes(), self.source.read_bytes())
        self.assertEqual((self.destination / "operator-held-partial" / self.source.name).read_bytes(), b"partial")

    def test_source_change_during_copy_does_not_publish_completion(self):
        real_run = hook.subprocess.run

        def copy_then_change(*args, **kwargs):
            result = real_run(*args, **kwargs)
            self.source.write_bytes(b"modified payload")
            return result

        with patch.object(hook.subprocess, "run", side_effect=copy_then_change):
            with self.assertRaisesRegex(RuntimeError, "Source changed"):
                self.copy()
        self.assertFalse((self.destination / "intake-job-safe-job" / hook.COMPLETE_MARKER).exists())

    def test_unsupported_marker_publication_fails_without_completion(self):
        with patch.object(hook.os, "link", side_effect=OSError("hard links unsupported")):
            with self.assertRaisesRegex(OSError, "hard links unsupported"):
                self.copy()
        folder = self.destination / "intake-job-safe-job"
        self.assertFalse((folder / hook.COMPLETE_MARKER).exists())
        self.assertTrue((folder / hook.PENDING_MARKER).exists())
        self.assertEqual(self.source.read_bytes(), b"scanned payload")


if __name__ == "__main__":
    unittest.main()
