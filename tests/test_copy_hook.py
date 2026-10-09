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


class RoutedCopyBase(CopyHookBase):
    def setUp(self):
        super().setUp()
        self.library = self.root / "Movies"
        self.library.mkdir()
        self.source = self.library / "movie.mkv"
        self.source.write_bytes(b"scanned payload")
        self.copy_root_patch = patch.object(hook, "COPY_ROOT", self.destination)
        self.copy_root_patch.start()
        self.addCleanup(self.copy_root_patch.stop)
        self.state = self.destination / hook.STATE_DIRECTORY / "safe-job"

    def copy_routed(self, source=None, *, relative_path=None, source_root=None, job_id="safe-job"):
        source = source or self.source
        source_root = source_root or self.library
        return hook.copy_promoted(
            source, "a" * 40, "Movie", job_id, destination=self.destination,
            source_root=source_root,
            relative_path=source.relative_to(source_root) if relative_path is None else relative_path,
        )


class RoutedCopySafetyTests(RoutedCopyBase):
    def test_requires_complete_routing_and_exact_relative_path(self):
        with self.assertRaisesRegex(RuntimeError, "both source root"):
            hook.copy_promoted(self.source, "a" * 40, "Movie", "safe-job", source_root=self.library)
        for relative in ("", ".", "../movie.mkv", "/movie.mkv", hook.STATE_DIRECTORY, hook.MOUNT_MARKER):
            with self.subTest(relative=relative), self.assertRaisesRegex(RuntimeError, "relative path"):
                self.copy_routed(relative_path=relative)
        with self.assertRaisesRegex(RuntimeError, "exactly match"):
            self.copy_routed(relative_path="another.mkv")

    def test_destination_requires_existing_marker_before_reservation(self):
        (self.destination / hook.MOUNT_MARKER).unlink()
        with patch.object(hook.subprocess, "run") as run, self.assertRaises(FileNotFoundError):
            self.copy_routed()
        run.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_mapped_parent_and_state_directory_must_not_be_symlinks(self):
        genre = self.library / "Genre"
        genre.mkdir()
        source = genre / "movie.mkv"
        source.write_bytes(b"video")
        (self.destination / "Genre").symlink_to(self.root, target_is_directory=True)
        with patch.object(hook.subprocess, "run") as run, self.assertRaisesRegex(RuntimeError, "Symbolic links"):
            self.copy_routed(source)
        run.assert_not_called()
        state_link = self.destination / hook.STATE_DIRECTORY / "other-job"
        state_link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "Symbolic links"):
            self.copy_routed(job_id="other-job")

    def test_source_tree_symlinks_and_special_files_are_rejected(self):
        source = self.library / "Series"
        source.mkdir()
        link = source / "link"
        link.symlink_to(self.source)
        with self.assertRaisesRegex(RuntimeError, "Symbolic links"):
            self.copy_routed(source)
        link.unlink()
        os.mkfifo(source / "pipe")
        with self.assertRaisesRegex(RuntimeError, "Only regular files"):
            self.copy_routed(source)
        self.assertFalse(self.state.exists())

    def test_bind_alias_of_source_directory_is_rejected(self):
        source = self.library / "Series"
        source.mkdir()
        real_checked_path = hook.checked_path
        with patch.object(hook, "checked_path", side_effect=lambda path: real_checked_path(source if path == self.destination else path)):
            with self.assertRaisesRegex(RuntimeError, "aliases the source"):
                self.copy_routed(source)
        self.assertFalse(self.state.exists())

    def test_existing_target_is_never_merged_or_overwritten(self):
        target = self.destination / self.source.name
        target.write_bytes(b"keep existing")
        with patch.object(hook.subprocess, "run") as run, self.assertRaisesRegex(RuntimeError, "Destination collision"):
            self.copy_routed()
        run.assert_not_called()
        self.assertEqual(target.read_bytes(), b"keep existing")
        self.assertEqual(list(self.state.glob("attempt-*")), [])
        self.assertFalse((self.state / "complete.json").exists())

    def test_existing_directory_is_not_merged_even_when_empty(self):
        source = self.library / "Series"
        source.mkdir()
        (source / "episode.mkv").write_bytes(b"video")
        target = self.destination / "Series"
        target.mkdir()
        with patch.object(hook.subprocess, "run") as run, self.assertRaisesRegex(RuntimeError, "Destination collision"):
            self.copy_routed(source)
        run.assert_not_called()
        self.assertEqual(list(target.iterdir()), [])

    def test_failed_directory_copy_leaves_partial_and_journal_for_manual_review(self):
        source = self.library / "Series"
        source.mkdir()
        (source / "episode.mkv").write_bytes(b"full video")

        def fail_copy(argv, **kwargs):
            (Path(argv[-1]) / "episode.mkv").write_bytes(b"partial")
            raise subprocess.CalledProcessError(23, argv)

        with patch.object(hook.subprocess, "run", side_effect=fail_copy), self.assertRaises(subprocess.CalledProcessError):
            self.copy_routed(source)
        self.assertEqual((self.destination / "Series" / "episode.mkv").read_bytes(), b"partial")
        self.assertEqual((source / "episode.mkv").read_bytes(), b"full video")
        self.assertFalse((self.state / "complete.json").exists())
        with patch.object(hook.subprocess, "run") as run, self.assertRaisesRegex(RuntimeError, "manual retry"):
            self.copy_routed(source)
        run.assert_not_called()

    def test_fixed_local_rsync_argv_for_directory_contents(self):
        source = self.library / "-title; $(not-a-command)"
        source.mkdir()
        with patch.object(hook.subprocess, "run", side_effect=RuntimeError("stop")) as run:
            with self.assertRaisesRegex(RuntimeError, "stop"):
                self.copy_routed(source)
        argv = run.call_args.args[0]
        self.assertEqual(argv[-3:], ["--", str(source) + "/", str(self.destination / source.name) + "/"])
        for forbidden in ("--delete", "--remove-source-files", "--links", "--copy-links", "-a", "-e"):
            self.assertNotIn(forbidden, argv)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_cli_passes_optional_routing_arguments(self):
        argv = ["copy_action.py", "--source", str(self.source), "--torrent-hash", "a" * 40,
                "--torrent-name", "Movie", "--job-id", "safe-job", "--destination", str(self.destination),
                "--source-root", str(self.library), "--relative-path", "movie.mkv"]
        with patch.object(sys, "argv", argv), patch.object(hook.os, "umask"), patch.object(hook, "copy_promoted") as copy:
            self.assertEqual(hook.main(), 0)
        copy.assert_called_once_with(self.source, "a" * 40, "Movie", "safe-job", destination=self.destination,
                                     source_root=self.library, relative_path=Path("movie.mkv"))

    def test_existing_same_size_different_content_is_not_success(self):
        target = self.destination / self.source.name
        target.write_bytes(b"x" * self.source.stat().st_size)
        with patch.object(hook.subprocess, "run") as run, self.assertRaisesRegex(RuntimeError, "Destination collision"):
            self.copy_routed()
        run.assert_not_called()
        self.assertEqual(target.read_bytes(), b"x" * self.source.stat().st_size)
        self.assertFalse((self.state / "complete.json").exists())

    def test_existing_payload_hardlink_alias_is_rejected(self):
        os.link(self.source, self.destination / self.source.name)
        with patch.object(hook.subprocess, "run") as run, self.assertRaisesRegex(RuntimeError, "aliases the source"):
            self.copy_routed()
        run.assert_not_called()

    def test_lock_symlink_and_special_file_are_rejected_without_touching_source(self):
        self.state.mkdir(parents=True)
        lock = self.state / "copy.lock"
        lock.symlink_to(self.source)
        with self.assertRaisesRegex(RuntimeError, "Symbolic links"):
            self.copy_routed()
        lock.unlink()
        os.mkfifo(lock)
        with self.assertRaisesRegex(RuntimeError, "regular file"):
            self.copy_routed()
        self.assertEqual(self.source.read_bytes(), b"scanned payload")

    def test_completion_symlink_is_rejected_even_when_payload_is_absent(self):
        self.state.mkdir(parents=True)
        (self.state / "complete.json").symlink_to(self.source)
        with patch.object(hook.subprocess, "run") as run, self.assertRaisesRegex(RuntimeError, "Symbolic links"):
            self.copy_routed()
        run.assert_not_called()
        self.assertEqual(self.source.read_bytes(), b"scanned payload")

    def test_parallel_same_job_and_unavailable_locks_fail_without_payload_writes(self):
        self.state.mkdir(parents=True)
        with hook._job_lock(self.state), patch.object(hook.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "already running"):
                self.copy_routed()
        run.assert_not_called()
        with patch.object(hook.fcntl, "flock", side_effect=OSError("not supported")):
            with self.assertRaisesRegex(RuntimeError, "lock is unavailable"):
                self.copy_routed()
        self.assertFalse((self.destination / self.source.name).exists())

    def test_source_or_target_mutation_during_existing_content_comparison_fails(self):
        target = self.destination / self.source.name
        actual_compare = hook._same_file_bytes
        for modified in (self.source, target):
            self.source.write_bytes(b"scanned payload")
            target.write_bytes(b"scanned payload")

            def compare_then_mutate(*args):
                result = actual_compare(*args)
                modified.write_bytes(b"changed payload")
                return result

            with self.subTest(modified=modified), patch.object(hook, "_same_file_bytes", side_effect=compare_then_mutate):
                with self.assertRaisesRegex(RuntimeError, "changed during content comparison"):
                    self.copy_routed()
            self.assertFalse((self.state / "complete.json").exists())

    def test_marker_change_during_existing_content_comparison_fails(self):
        target = self.destination / self.source.name
        target.write_bytes(self.source.read_bytes())
        actual_compare = hook._same_file_bytes

        def compare_then_change_marker(*args):
            result = actual_compare(*args)
            (self.destination / hook.MOUNT_MARKER).write_text("replaced")
            return result

        with patch.object(hook, "_same_file_bytes", side_effect=compare_then_change_marker):
            with self.assertRaisesRegex(RuntimeError, "marker changed"):
                self.copy_routed()
        self.assertFalse((self.state / "complete.json").exists())


@unittest.skipUnless(shutil.which("rsync"), "rsync is not installed; real-copy integration checks skipped")
class RoutedCopyRsyncTests(RoutedCopyBase):
    def setUp(self):
        super().setUp()
        self.rsync_patch = patch.object(hook, "RSYNC", shutil.which("rsync"))
        self.rsync_patch.start()
        self.addCleanup(self.rsync_patch.stop)

    def test_directory_preserves_nested_structure_without_job_wrapper_or_payload_marker(self):
        source = self.library / "Genre" / "Example Film"
        source.mkdir(parents=True)
        (source / "disc").mkdir()
        (source / "empty").mkdir()
        (source / "disc" / "film.mkv").write_bytes(b"video")
        # In particular, successful copying must not depend on NFS supporting
        # the renameat2(RENAME_NOREPLACE) operation that failed in older movers.
        with patch.object(hook.os, "rename", side_effect=OSError(95, "unsupported")), \
                patch.object(hook.os, "replace", side_effect=OSError(95, "unsupported")):
            copied = self.copy_routed(source)
        self.assertEqual(copied, self.destination / "Genre" / "Example Film")
        self.assertEqual((copied / "disc" / "film.mkv").read_bytes(), b"video")
        self.assertEqual(hook.layout(hook.snapshot(source)), hook.layout(hook.snapshot(copied)))
        self.assertFalse((self.destination / "intake-job-safe-job").exists())
        self.assertFalse((copied / hook.COMPLETE_MARKER).exists())
        self.assertFalse((self.destination / self.source.name).exists())
        self.assertTrue((self.state / "complete.json").is_file())
        with patch.object(hook.subprocess, "run") as run:
            self.assertEqual(self.copy_routed(source), copied)
        run.assert_not_called()

    def test_single_file_preserves_nested_path_and_completed_retry_is_noop(self):
        source = self.library / "Genre" / "literal: title $(not-a-command).mkv"
        source.parent.mkdir()
        source.write_bytes(b"video")
        copied = self.copy_routed(source)
        self.assertEqual(copied, self.destination / source.relative_to(self.library))
        self.assertEqual(copied.read_bytes(), b"video")
        self.assertEqual(list(next(self.state.glob("attempt-*/payload")).iterdir()), [])
        self.assertTrue(source.exists())
        with patch.object(hook.subprocess, "run") as run:
            self.assertEqual(self.copy_routed(source), copied)
        run.assert_not_called()

    def test_completed_copy_mutation_fails_instead_of_overwrite(self):
        copied = self.copy_routed()
        copied.write_bytes(b"modified")
        with patch.object(hook.subprocess, "run") as run, self.assertRaisesRegex(RuntimeError, "manual retry"):
            self.copy_routed()
        run.assert_not_called()
        self.assertEqual(copied.read_bytes(), b"modified")

    def test_source_change_prevents_single_file_publication(self):
        real_run = hook.subprocess.run

        def copy_then_change(*args, **kwargs):
            result = real_run(*args, **kwargs)
            self.source.write_bytes(b"changed")
            return result

        with patch.object(hook.subprocess, "run", side_effect=copy_then_change):
            with self.assertRaisesRegex(RuntimeError, "Source changed"):
                self.copy_routed()
        self.assertFalse((self.destination / self.source.name).exists())
        self.assertFalse((self.state / "complete.json").exists())

    def test_marker_change_prevents_completion(self):
        real_run = hook.subprocess.run

        def copy_then_change(*args, **kwargs):
            result = real_run(*args, **kwargs)
            (self.destination / hook.MOUNT_MARKER).write_text("replaced")
            return result

        with patch.object(hook.subprocess, "run", side_effect=copy_then_change):
            with self.assertRaisesRegex(RuntimeError, "marker changed"):
                self.copy_routed()
        self.assertFalse((self.state / "complete.json").exists())

    def test_file_publication_collision_never_overwrites_new_destination(self):
        real_run = hook.subprocess.run
        copied = self.destination / self.source.name

        def copy_then_collide(*args, **kwargs):
            result = real_run(*args, **kwargs)
            copied.write_bytes(b"another writer")
            return result

        with patch.object(hook.subprocess, "run", side_effect=copy_then_collide), self.assertRaises(FileExistsError):
            self.copy_routed()
        self.assertEqual(copied.read_bytes(), b"another writer")
        self.assertFalse((self.state / "complete.json").exists())

    def test_unsupported_hardlink_publication_retains_source_and_private_copy(self):
        with patch.object(hook.os, "link", side_effect=OSError("hard links unsupported")):
            with self.assertRaisesRegex(OSError, "hard links unsupported"):
                self.copy_routed()
        self.assertFalse((self.destination / self.source.name).exists())
        self.assertFalse((self.state / "complete.json").exists())
        staged = next(self.state.glob("attempt-*/payload")) / self.source.name
        self.assertEqual(staged.read_bytes(), self.source.read_bytes())

    def test_manual_retry_after_both_partial_payload_and_state_are_moved_aside(self):
        copied = self.destination / self.source.name
        copied.write_bytes(b"partial")
        self.state.mkdir(parents=True)
        (self.state / "request.json").write_text("{}")
        copied.rename(self.destination / "operator-held-partial")
        self.state.rename(self.state.parent / "operator-held-state")
        self.assertEqual(self.copy_routed(), copied)
        self.assertEqual(copied.read_bytes(), b"scanned payload")
        self.assertEqual((self.destination / "operator-held-partial").read_bytes(), b"partial")

    def test_delete_both_payloads_then_readd_same_and_new_job(self):
        copied = self.copy_routed()
        receipt = (self.state / "complete.json").read_bytes()
        self.source.unlink()
        copied.unlink()
        self.source.write_bytes(b"scanned payload")
        self.assertEqual(self.copy_routed(), copied)
        self.assertEqual(copied.read_bytes(), b"scanned payload")
        self.assertIn(receipt, [path.read_bytes() for path in self.state.glob("attempt-*/previous-complete.json")])
        self.assertEqual(len(list(self.state.glob("attempt-*"))), 2)
        self.source.unlink()
        copied.unlink()
        self.source.write_bytes(b"scanned payload")
        self.assertEqual(self.copy_routed(job_id="readded-job"), copied)
        self.assertEqual(copied.read_bytes(), b"scanned payload")
        self.assertTrue((self.state.parent / "readded-job" / "complete.json").exists())

    def test_existing_identical_target_from_other_job_is_reused_without_media_writes(self):
        copied = self.copy_routed()
        original = hook.snapshot(copied)
        with patch.object(hook.subprocess, "run") as run, patch.object(hook, "_same_file_bytes", wraps=hook._same_file_bytes) as compare:
            self.assertEqual(self.copy_routed(job_id="another-job"), copied)
        run.assert_not_called()
        compare.assert_called_once()
        self.assertEqual(hook.snapshot(copied), original)
        self.assertTrue((self.state.parent / "another-job" / "complete.json").exists())

    def test_identical_nested_directory_is_reused_but_extra_files_are_not_merged(self):
        source = self.library / "Series"
        (source / "Season 1").mkdir(parents=True)
        (source / "empty").mkdir()
        (source / "Season 1" / "episode.mkv").write_bytes(b"episode")
        copied = self.copy_routed(source)
        original = hook.snapshot(copied)
        with patch.object(hook.subprocess, "run") as run:
            self.assertEqual(self.copy_routed(source, job_id="directory-readd"), copied)
        run.assert_not_called()
        self.assertEqual(hook.snapshot(copied), original)
        extra = copied / "user-notes.txt"
        extra.write_text("keep this")
        with patch.object(hook.subprocess, "run") as run, self.assertRaisesRegex(RuntimeError, "Destination collision"):
            self.copy_routed(source, job_id="directory-extra")
        run.assert_not_called()
        self.assertEqual(extra.read_text(), "keep this")

    def test_legacy_pending_only_journal_does_not_block_absent_payload(self):
        self.state.mkdir(parents=True)
        (self.state / "request.json").write_text("{old request}")
        (self.state / "complete.pending").write_text("unfinished old metadata")
        work = self.state / "payload"
        work.mkdir()
        (work / self.source.name).write_bytes(b"partial")
        copied = self.copy_routed()
        self.assertEqual(copied.read_bytes(), self.source.read_bytes())
        self.assertEqual((work / self.source.name).read_bytes(), b"partial")
        self.assertEqual((self.state / "complete.pending").read_text(), "unfinished old metadata")

    def test_changed_stat_but_identical_bytes_revalidates_and_preserves_old_receipt(self):
        copied = self.copy_routed()
        old_receipt = (self.state / "complete.json").read_bytes()
        self.source.rename(self.library / "old-file")
        self.source.write_bytes(b"scanned payload")
        target_before = hook.snapshot(copied)
        with patch.object(hook.subprocess, "run") as run, patch.object(hook, "_same_file_bytes", wraps=hook._same_file_bytes) as compare:
            self.assertEqual(self.copy_routed(), copied)
        run.assert_not_called()
        compare.assert_called_once()
        self.assertEqual(hook.snapshot(copied), target_before)
        self.assertIn(old_receipt, [path.read_bytes() for path in self.state.glob("attempt-*/previous-complete.json")])

    def test_failed_collision_can_retry_after_only_colliding_payload_removed(self):
        copied = self.destination / self.source.name
        copied.write_bytes(b"other payload")
        with self.assertRaisesRegex(RuntimeError, "Destination collision"):
            self.copy_routed()
        copied.unlink()
        self.assertEqual(self.copy_routed(), copied)
        self.assertEqual(copied.read_bytes(), self.source.read_bytes())

    def test_failed_private_staging_is_retained_and_never_reused(self):
        def fail_copy(argv, **kwargs):
            (Path(argv[-1]) / self.source.name).write_bytes(b"partial")
            raise subprocess.CalledProcessError(23, argv)

        with patch.object(hook.subprocess, "run", side_effect=fail_copy), self.assertRaises(subprocess.CalledProcessError):
            self.copy_routed()
        old_payload = next(self.state.glob("attempt-*/payload")) / self.source.name
        self.assertFalse((self.destination / self.source.name).exists())
        copied = self.copy_routed()
        self.assertEqual(copied.read_bytes(), self.source.read_bytes())
        self.assertEqual(old_payload.read_bytes(), b"partial")
        self.assertEqual(len(list(self.state.glob("attempt-*"))), 2)

    def test_removed_partial_directory_can_retry_without_removing_journal(self):
        source = self.library / "Series"
        source.mkdir()
        (source / "episode.mkv").write_bytes(b"full video")

        def fail_copy(argv, **kwargs):
            (Path(argv[-1]) / "episode.mkv").write_bytes(b"partial")
            raise subprocess.CalledProcessError(23, argv)

        with patch.object(hook.subprocess, "run", side_effect=fail_copy), self.assertRaises(subprocess.CalledProcessError):
            self.copy_routed(source)
        copied = self.destination / source.name
        copied.rename(self.destination / "operator-held-partial")
        self.assertEqual(self.copy_routed(source), copied)
        self.assertEqual((copied / "episode.mkv").read_bytes(), b"full video")
        self.assertEqual((self.destination / "operator-held-partial" / "episode.mkv").read_bytes(), b"partial")
        self.assertEqual(len(list(self.state.glob("attempt-*"))), 2)

    def test_old_format_receipt_and_staging_are_compatible_and_preserved(self):
        copied = self.destination / self.source.name
        copied.write_bytes(self.source.read_bytes())
        self.state.mkdir(parents=True)
        request = {"source": str(self.source), "source_root": str(self.library),
                   "relative_path": self.source.name, "destination": str(self.destination),
                   "torrent_hash": "a" * 40, "torrent_name": "Movie", "job_id": "safe-job"}
        record = {"request": request, "source_snapshot": hook.snapshot(self.source),
                  "destination_snapshot": hook.snapshot(copied)}
        (self.state / "complete.json").write_text(json.dumps(record))
        (self.state / "request.json").write_text(json.dumps(request))
        (self.state / "complete.pending").write_text("old pending receipt")
        old_work = self.state / "payload"
        old_work.mkdir()
        (old_work / self.source.name).write_bytes(b"stale private payload")
        with patch.object(hook.subprocess, "run") as run, patch.object(hook, "_same_file_bytes") as compare:
            self.assertEqual(self.copy_routed(), copied)
        run.assert_not_called()
        compare.assert_not_called()
        copied.unlink()
        self.assertEqual(self.copy_routed(), copied)
        self.assertEqual((old_work / self.source.name).read_bytes(), b"stale private payload")
        self.assertEqual((self.state / "complete.pending").read_text(), "old pending receipt")
        self.assertEqual(copied.read_bytes(), self.source.read_bytes())
        archived = next(self.state.glob("attempt-*/previous-complete.json"))
        self.assertEqual(json.loads(archived.read_text()), record)

    def test_swapping_mapped_roots_and_back_reuses_identical_payloads(self):
        # Directory renames simulate Docker repointing stable container paths to
        # opposite host directories, without requiring privileged bind mounts.
        (self.library / hook.MOUNT_MARKER).touch()
        self.copy_routed()

        def swap_roots():
            temporary = self.root / "swap-holding"
            self.library.rename(temporary)
            self.destination.rename(self.library)
            temporary.rename(self.destination)

        for _ in range(2):
            swap_roots()
            target_before = hook.snapshot(self.destination / self.source.name)
            with patch.object(hook.subprocess, "run") as run:
                copied = self.copy_routed()
            run.assert_not_called()
            self.assertEqual(hook.snapshot(copied), target_before)
            self.assertEqual(copied.read_bytes(), self.source.read_bytes())

    def test_source_and_destination_root_change_during_copy_prevents_completion(self):
        actual_run, actual_checked = hook.subprocess.run, hook.checked_path
        substitute = self.root / "different-mount"
        substitute.mkdir()
        changed = False

        def run_then_remount(*args, **kwargs):
            nonlocal changed
            result = actual_run(*args, **kwargs)
            changed = True
            return result

        for index, switched_root in enumerate((self.library, self.destination)):
            changed = False

            def remounted_stat(path):
                return actual_checked(substitute if changed and path == switched_root else path)

            with self.subTest(root=switched_root), patch.object(hook.subprocess, "run", side_effect=run_then_remount), \
                    patch.object(hook, "checked_path", side_effect=remounted_stat):
                with self.assertRaisesRegex(RuntimeError, "mount changed"):
                    self.copy_routed(job_id=f"remount-{index}")
            self.assertFalse((self.destination / self.source.name).exists())
            self.assertFalse((self.state.parent / f"remount-{index}" / "complete.json").exists())


if __name__ == "__main__":
    unittest.main()
