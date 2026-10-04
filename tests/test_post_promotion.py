from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Job, ScanFile
from app.post_promotion import OUTPUT_LIMIT_BYTES, PostPromotionRunner, queue_promotion_hook
from app.service import JobService


class PostPromotionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        # CI deliberately keeps /tmp non-executable. Its separate /hooks tmpfs
        # models the production executable mount without weakening that policy.
        hook_parent = "/hooks" if Path("/hooks").is_dir() and os.access("/hooks", os.W_OK | os.X_OK) else None
        self.hook_temporary = tempfile.TemporaryDirectory(prefix="ti-hook-tests-", dir=hook_parent)
        self.hooks = Path(self.hook_temporary.name)
        self.final = self.root / "downloads" / "Movies"
        self.final.mkdir(parents=True)
        self.source = self.final / "movie $(touch never); 'quoted'"
        self.source.mkdir()
        (self.source / "video.mkv").write_bytes(b"clean media")
        self.script = self.hooks / "process.sh"
        self.script.write_text("#!/bin/sh\nprintf 'normal output\\n'\nprintf 'stderr output\\n' >&2\n", encoding="utf-8")
        self.script.chmod(0o700)
        self.settings = SimpleNamespace(
            post_promotion_enabled=True,
            post_promotion_script=str(self.script),
            post_promotion_copy_enabled=False,
            post_promotion_copy_destination=None,
            post_promotion_delay_seconds=5,
            post_promotion_timeout_seconds=5,
            completion_grace_seconds=15,
            allowed_final_parent_prefixes=[str(self.root / "downloads")],
            local_staging_root=str(self.root / "staging-local"),
            nas_staging_root=str(self.root / "downloads" / "staging"),
            effective_nas_locations=[],
        )
        self.engine = create_engine(f"sqlite:///{self.root / 'test.db'}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.torrent = SimpleNamespace(
            hash="a" * 40, tags="torrent_intake, ti_job_hook-job", progress=1.0,
            amount_left=0, state="uploading", save_path=str(self.final), content_path=str(self.source),
        )
        self.qbt = SimpleNamespace(get_torrent=lambda _hash: self.torrent)
        self.runner = PostPromotionRunner(settings=self.settings, qbt=self.qbt, session_factory=self.sessions)
        self.root_patch = patch("app.post_promotion.HOOK_ROOT", self.hooks)
        self.root_patch.start()
        self.event_patch = patch("app.post_promotion.emit_event")
        self.event = self.event_patch.start()

    def tearDown(self) -> None:
        self.event_patch.stop()
        self.root_patch.stop()
        self.engine.dispose()
        self.hook_temporary.cleanup()
        self.temporary.cleanup()

    def job(self, *, queued: bool = True, due: bool = True) -> str:
        with self.sessions() as db:
            job = Job(
                id="hook-job", magnet_uri="magnet:?xt=urn:btih:" + "a" * 40,
                final_parent=str(self.final), staging_preference="local",
                staging_root_initial=self.settings.local_staging_root,
                managed_tag="torrent_intake", unique_tag="ti_job_hook-job",
                qbt_hash="a" * 40, torrent_name="name $(touch injected); 'quotes' & more",
                state="done", is_terminal=True, content_path=str(self.source),
                scan_completed_at=datetime.utcnow(), promoted_at=datetime.utcnow(),
            )
            if queued:
                queue_promotion_hook(job, self.settings)
                if due:
                    job.hook_due_at = datetime.utcnow() - timedelta(seconds=1)
            db.add(job)
            db.add(ScanFile(
                job_id=job.id, relative_path="video.mkv", size_bytes=11, mtime_ns=0, status="clean",
            ))
            db.commit()
            return job.id

    def record(self) -> Job:
        with self.sessions() as db:
            job = db.get(Job, "hook-job")
            db.expunge(job)
            return job

    def update(self, **values) -> None:
        with self.sessions() as db:
            job = db.get(Job, "hook-job")
            for name, value in values.items():
                setattr(job, name, value)
            db.commit()

    def test_disabled_does_not_enqueue_or_backfill_history(self) -> None:
        self.settings.post_promotion_enabled = False
        self.job()
        self.assertIsNone(self.record().hook_status)
        self.settings.post_promotion_enabled = True
        self.assertIsNone(self.runner.claim_next())
        self.assertIsNone(self.record().hook_status)

    def enable_copy(self) -> Path:
        destination = self.root / "copy-target"
        destination.mkdir()
        (destination / ".intake-copy-mount").touch()
        self.settings.post_promotion_enabled = False
        self.settings.post_promotion_script = None
        self.settings.post_promotion_copy_enabled = True
        self.settings.post_promotion_copy_destination = str(destination)
        copy_root_patch = patch("app.copy_action.COPY_ROOT", destination)
        copy_root_patch.start()
        self.addCleanup(copy_root_patch.stop)
        return destination

    def test_copy_is_pinned_and_does_not_need_any_script(self) -> None:
        destination = self.enable_copy()
        self.job()
        self.settings.post_promotion_copy_destination = str(destination / "new-default")
        claim = self.runner.claim_next()
        self.assertEqual(claim.kind, "copy")
        self.assertEqual(claim.destination, str(destination))
        self.assertIsNone(claim.script)
        self.assertEqual(claim.argv[:5], [sys.executable, "-m", "app.copy_action", "--destination", str(destination)])
        self.assertIn("--torrent-name=" + self.record().torrent_name, claim.argv)
        self.assertEqual(self.record().hook_kind, "copy")
        self.assertEqual(self.record().hook_destination, str(destination))

    def test_copy_retry_preserves_target_and_cannot_change_to_script(self) -> None:
        destination = self.enable_copy()
        self.job()
        self.settings.post_promotion_copy_destination = str(destination / "new-default")
        with self.sessions() as db:
            job = db.get(Job, "hook-job")
            job.hook_status = None
            job.hook_attempts = 1
            self.assertTrue(queue_promotion_hook(job, self.settings))
            self.assertEqual(job.hook_destination, str(destination))
            self.assertEqual(job.hook_attempts, 1)
            job.hook_status = None
            self.settings.post_promotion_copy_enabled = False
            self.settings.post_promotion_enabled = True
            self.settings.post_promotion_script = str(self.script)
            self.assertFalse(queue_promotion_hook(job, self.settings))

    def test_missing_copy_marker_defers_without_claim_or_manifest_walk(self) -> None:
        destination = self.enable_copy()
        self.job()
        (destination / ".intake-copy-mount").unlink()
        with patch("app.post_promotion._filesystem_manifest") as manifest:
            self.assertIsNone(self.runner.claim_next())
        manifest.assert_not_called()
        self.assertEqual(self.record().hook_status, "pending")
        self.assertEqual(self.record().hook_attempts, 0)
        self.assertIn("copy destination/mount", self.record().hook_error)
        self.assertGreater(self.record().hook_due_at, datetime.utcnow())
        (destination / ".intake-copy-mount").touch()
        self.update(hook_due_at=datetime.utcnow() - timedelta(seconds=1))
        self.assertIsNotNone(self.runner.claim_next())

    def test_missing_copy_target_defers_without_creating_directory(self) -> None:
        destination = self.enable_copy()
        (destination / ".intake-copy-mount").unlink()
        destination.rmdir()
        self.job()
        self.assertIsNone(self.runner.claim_next())
        self.assertEqual(self.record().hook_status, "pending")
        self.assertFalse(destination.exists())

    def test_tampered_copy_target_fails_without_side_effects(self) -> None:
        self.enable_copy()
        self.job()
        self.update(hook_destination=str(self.final))
        self.assertIsNone(self.runner.claim_next())
        self.assertEqual(self.record().hook_status, "failed")
        self.assertIn("/copy-target", self.record().hook_error)
        self.assertEqual(self.record().hook_attempts, 0)

    def test_copy_still_requires_unchanged_clean_manifest(self) -> None:
        destination = self.enable_copy()
        self.job()
        (self.source / "new-unscanned.exe").write_bytes(b"not scanned")
        self.assertIsNone(self.runner.claim_next())
        self.assertEqual(self.record().hook_status, "failed")
        self.assertIn("manifest", self.record().hook_error)
        self.assertEqual(list(destination.iterdir()), [destination / ".intake-copy-mount"])

    def test_switching_action_mode_does_not_execute_old_pending_work(self) -> None:
        self.job()
        self.enable_copy()
        self.assertIsNone(self.runner.claim_next())
        self.assertEqual(self.record().hook_status, "pending")

    def test_legacy_pending_script_without_kind_remains_compatible(self) -> None:
        self.job()
        self.update(hook_kind=None)
        claim = self.runner.claim_next()
        self.assertEqual(claim.kind, "script")
        self.assertEqual(claim.script, str(self.script))

    async def test_builtin_copy_executes_without_executable_hook_mount(self) -> None:
        target_root = Path("/copy-target")
        if not target_root.is_dir() or not os.access(target_root, os.W_OK | os.X_OK):
            self.skipTest("/copy-target writable test mount is required for subprocess integration")
        with tempfile.TemporaryDirectory(prefix="ti-copy-test-", dir=target_root) as name:
            destination = Path(name)
            (destination / ".intake-copy-mount").touch()
            self.settings.post_promotion_enabled = False
            self.settings.post_promotion_script = None
            self.settings.post_promotion_copy_enabled = True
            self.settings.post_promotion_copy_destination = name
            self.job()
            self.script.unlink()
            claim = self.runner.claim_next()
            self.assertIsNotNone(claim)
            await self.runner.run_claim(claim, asyncio.Event())
            job = self.record()
            self.assertEqual(job.hook_status, "succeeded", job.hook_output)
            copied = destination / "intake-job-hook-job" / self.source.name / "video.mkv"
            self.assertEqual(copied.read_bytes(), b"clean media")
            self.assertTrue((self.source / "video.mkv").exists())
            self.assertTrue((copied.parent.parent / ".intake-copy-complete.json").is_file())
    def test_disabled_runner_preserves_existing_pending_work_until_enabled(self) -> None:
        self.job()
        self.settings.post_promotion_enabled = False
        self.assertEqual(self.runner.recover_interrupted(), 0)
        self.assertIsNone(self.runner.claim_next())
        self.assertEqual(self.record().hook_status, "pending")
        self.assertEqual(self.record().hook_attempts, 0)
        self.settings.post_promotion_enabled = True
        self.assertIsNotNone(self.runner.claim_next())

    def test_disabled_runner_still_recovers_ambiguous_running_attempt(self) -> None:
        self.job()
        self.assertIsNotNone(self.runner.claim_next())
        self.settings.post_promotion_enabled = False
        self.assertEqual(self.runner.recover_interrupted(), 1)
        self.assertEqual(self.record().hook_status, "interrupted")
        self.assertIsNone(self.runner.claim_next())

    def test_queue_refuses_infected_failed_or_unscanned_jobs(self) -> None:
        self.job(queued=False)
        with self.sessions() as db:
            job = db.get(Job, "hook-job")
            for state in ("infected_held", "infected_quarantined", "infected_deleted", "error", "scan_clean", "promoting"):
                with self.subTest(state=state):
                    job.state = state
                    self.assertFalse(queue_promotion_hook(job, self.settings))
                    self.assertIsNone(job.hook_status)
            job.state = "done"
            job.scan_completed_at = None
            self.assertFalse(queue_promotion_hook(job, self.settings))
            job.scan_completed_at = datetime.utcnow()
            job.promoted_at = None
            self.assertFalse(queue_promotion_hook(job, self.settings))

    def promotion_service(self) -> JobService:
        service = JobService()
        service.settings = self.settings
        service.qbt = self.qbt
        self.qbt.resume = Mock()
        return service

    def test_verified_promotion_and_pending_hook_share_one_commit(self) -> None:
        self.job(queued=False)
        self.update(state="promoting", is_terminal=False, promoted_at=None)
        self.torrent.state = "pausedUP"
        service = self.promotion_service()
        with self.sessions() as db:
            job = db.get(Job, "hook-job")
            with patch.object(db, "commit", wraps=db.commit) as commit:
                self.assertTrue(service._reconcile_clean_promotion(db, job))
            commit.assert_called_once()
        job = self.record()
        self.assertEqual(job.state, "done")
        self.assertEqual(job.hook_status, "pending")
        self.assertEqual(job.hook_script, str(self.script))
        self.assertGreater(job.hook_due_at, job.promoted_at)
        self.qbt.resume.assert_called_once_with(job.qbt_hash)

    def test_failed_promotion_commit_cannot_leave_a_separately_queued_hook(self) -> None:
        self.job(queued=False)
        self.update(state="promoting", is_terminal=False, promoted_at=None)
        self.torrent.state = "pausedUP"
        service = self.promotion_service()
        with self.sessions() as db:
            job = db.get(Job, "hook-job")
            with patch.object(db, "commit", side_effect=RuntimeError("disk unavailable")):
                with self.assertRaisesRegex(RuntimeError, "disk unavailable"):
                    service._reconcile_clean_promotion(db, job)
            db.rollback()
        self.assertEqual(self.record().state, "promoting")
        self.assertIsNone(self.record().hook_status)
        self.assertIsNone(self.record().promoted_at)

    def test_promotion_still_moving_does_not_enqueue_hook(self) -> None:
        self.job(queued=False)
        self.update(state="promoting", is_terminal=False, promoted_at=None)
        self.torrent.state = "moving"
        with self.sessions() as db:
            self.assertFalse(self.promotion_service()._reconcile_clean_promotion(db, db.get(Job, "hook-job")))
        self.assertIsNone(self.record().hook_status)

    def test_delay_and_atomic_claim_prevent_early_or_duplicate_execution(self) -> None:
        self.job(due=False)
        self.assertIsNone(self.runner.claim_next())
        self.assertEqual(self.record().hook_attempts, 0)
        self.update(hook_due_at=datetime.utcnow() - timedelta(seconds=1))
        claim = self.runner.claim_next()
        self.assertIsNotNone(claim)
        self.assertEqual(self.record().hook_status, "running")
        self.assertEqual(self.record().hook_attempts, 1)
        self.assertIsNone(self.runner.claim_next())

    def test_live_torrent_incomplete_or_moving_stays_pending(self) -> None:
        self.job()
        for state, progress in (("moving", 1.0), ("downloading", 0.9), ("checkingDL", 1.0), ("checkingUP", 1.0)):
            with self.subTest(state=state):
                self.torrent.state, self.torrent.progress = state, progress
                self.update(hook_due_at=datetime.utcnow() - timedelta(seconds=1))
                self.assertIsNone(self.runner.claim_next())
                job = self.record()
                self.assertEqual(job.hook_status, "pending")
                self.assertEqual(job.hook_attempts, 0)
                self.assertIn("incomplete", job.hook_error)
                self.assertEqual(job.state, "done")

    def test_missing_mount_or_unavailable_qbt_stays_pending(self) -> None:
        self.job()
        missing = self.final / "temporarily-offline"
        self.source.rename(missing)
        self.assertIsNone(self.runner.claim_next())
        self.assertEqual(self.record().hook_status, "pending")
        self.assertIn("unavailable", self.record().hook_error)
        missing.rename(self.source)
        self.qbt.get_torrent = lambda _hash: None
        self.update(hook_due_at=datetime.utcnow() - timedelta(seconds=1))
        self.assertIsNone(self.runner.claim_next())
        self.assertIn("qBittorrent", self.record().hook_error)

    def test_missing_ownership_tag_fails_without_changing_promoted_job(self) -> None:
        self.job()
        self.torrent.tags = "torrent_intake"
        self.assertIsNone(self.runner.claim_next())
        job = self.record()
        self.assertEqual(job.hook_status, "failed")
        self.assertIn("tags are missing", job.hook_error)
        self.assertEqual(job.hook_attempts, 0)
        self.assertEqual(job.state, "done")
        self.assertTrue(job.is_terminal)

    def test_missing_registered_nas_mount_marker_stays_pending(self) -> None:
        self.job()
        marker = self.root / "downloads" / ".nas-online"
        self.settings.effective_nas_locations = [SimpleNamespace(
            path=self.settings.nas_staging_root, mount_marker=str(marker),
        )]
        self.assertIsNone(self.runner.claim_next())
        self.assertEqual(self.record().hook_status, "pending")
        self.assertIn("mount marker", self.record().hook_error)
        marker.write_text("mounted", encoding="utf-8")
        self.update(hook_due_at=datetime.utcnow() - timedelta(seconds=1))
        self.assertIsNotNone(self.runner.claim_next())

    def test_changed_manifest_and_symlinks_fail_closed(self) -> None:
        self.job()
        (self.source / "unscanned.txt").write_text("new file", encoding="utf-8")
        self.assertIsNone(self.runner.claim_next())
        self.assertIn("manifest", self.record().hook_error)
        (self.source / "unscanned.txt").unlink()
        (self.source / "link").symlink_to(self.source / "video.mkv")
        self.update(hook_status="pending", hook_due_at=datetime.utcnow() - timedelta(seconds=1))
        self.assertIsNone(self.runner.claim_next())
        self.assertIn("symbolic link", self.record().hook_error)

    def test_changed_torrent_path_fails_closed(self) -> None:
        self.job()
        self.torrent.content_path = str(self.final / "another-torrent")
        self.assertIsNone(self.runner.claim_next())
        self.assertIn("path changed", self.record().hook_error)

    def test_untrusted_or_changed_deployment_script_fails_closed(self) -> None:
        self.job()
        self.update(hook_script="/bin/sh")
        self.assertIsNone(self.runner.claim_next())
        self.assertIn("deployment script", self.record().hook_error)

    async def test_normal_output_and_shell_characters_are_passed_literally(self) -> None:
        self.script.write_text(
            f"#!{sys.executable}\nimport json, os, sys\n"
            "print(json.dumps({'args': sys.argv[1:], 'env': sorted(os.environ)}))\n"
            "print('stderr output', file=sys.stderr)\n", encoding="utf-8",
        )
        self.job()
        claim = self.runner.claim_next()
        with patch.dict(os.environ, {"TI_QBT_PASSWORD": "must-not-leak"}):
            await self.runner.run_claim(claim, asyncio.Event())
        job = self.record()
        self.assertEqual(job.hook_status, "succeeded")
        self.assertEqual(job.hook_exit_code, 0)
        self.assertIn("stderr output", job.hook_output)
        payload = next(json.loads(line) for line in job.hook_output.splitlines() if line.startswith("{"))
        self.assertEqual(payload["args"], claim.argv[1:])
        self.assertIn("--torrent-name=" + job.torrent_name, payload["args"])
        self.assertNotIn("TI_QBT_PASSWORD", payload["env"])
        self.assertEqual(job.state, "done")
        self.assertIsNone(self.runner.claim_next())
        self.event.assert_called_once()

    async def test_leading_dash_torrent_name_remains_one_argument_value(self) -> None:
        self.script.write_text(
            f"#!{sys.executable}\nimport argparse\n"
            "p = argparse.ArgumentParser()\n"
            "for name in ('source', 'torrent-hash', 'torrent-name', 'job-id'): p.add_argument('--' + name, required=True)\n"
            "print(p.parse_args().torrent_name)\n",
            encoding="utf-8",
        )
        self.job()
        name = "-Movie $(touch injected); 'quotes'"
        self.update(torrent_name=name)
        await self.runner.run_claim(self.runner.claim_next(), asyncio.Event())
        self.assertEqual(self.record().hook_status, "succeeded")
        self.assertEqual(self.record().hook_output.strip(), name)

    async def test_nonzero_exit_fails_without_rescan_or_automatic_retry(self) -> None:
        self.script.write_text("#!/bin/sh\nprintf 'problem\\n' >&2\nexit 7\n", encoding="utf-8")
        self.job()
        await self.runner.run_claim(self.runner.claim_next(), asyncio.Event())
        job = self.record()
        self.assertEqual(job.hook_status, "failed")
        self.assertEqual(job.hook_exit_code, 7)
        self.assertIn("problem", job.hook_output)
        self.assertEqual(job.state, "done")
        self.assertIsNone(self.runner.claim_next())

    async def test_timeout_is_bounded_and_keeps_successful_promotion(self) -> None:
        self.settings.post_promotion_timeout_seconds = 0.1
        self.script.write_text("#!/bin/sh\nprintf 'started\\n'\nsleep 20\n", encoding="utf-8")
        self.job()
        await asyncio.wait_for(self.runner.run_claim(self.runner.claim_next(), asyncio.Event()), 5)
        job = self.record()
        self.assertEqual(job.hook_status, "failed")
        self.assertIn("timed out", job.hook_error)
        self.assertIn("started", job.hook_output)
        self.assertEqual(job.state, "done")
        self.assertIsNone(self.runner.claim_next())

    async def test_output_capture_is_bounded(self) -> None:
        self.script.write_text(f"#!{sys.executable}\nprint('x' * 300000)\n", encoding="utf-8")
        self.job()
        await self.runner.run_claim(self.runner.claim_next(), asyncio.Event())
        job = self.record()
        self.assertEqual(job.hook_status, "succeeded")
        self.assertLessEqual(len(job.hook_output), OUTPUT_LIMIT_BYTES)
        self.assertIn("output truncated", job.hook_output)

    async def test_controller_stop_kills_process_group_and_records_interrupted(self) -> None:
        self.script.write_text("#!/bin/sh\nsleep 20 &\nprintf 'child:%s\\n' \"$!\"\nwait\n", encoding="utf-8")
        self.job()
        stop = asyncio.Event()
        task = asyncio.create_task(self.runner.run_claim(self.runner.claim_next(), stop))
        await asyncio.sleep(0.1)
        stop.set()
        await asyncio.wait_for(task, 5)
        job = self.record()
        self.assertEqual(job.hook_status, "interrupted")
        self.assertIsNotNone(job.hook_finished_at)
        self.assertEqual(job.state, "done")
        self.assertIsNone(self.runner.claim_next())
        child_pid = int(job.hook_output.split("child:")[1].splitlines()[0])
        child_status = Path(f"/proc/{child_pid}/stat")
        # An init process may not yet have reaped the dead grandchild, but it
        # must not still be running after the controller reports drained.
        if child_status.exists():
            self.assertEqual(child_status.read_text().split(") ", 1)[1].split()[0], "Z")

    async def test_runner_cancellation_records_interrupted(self) -> None:
        self.script.write_text("#!/bin/sh\nsleep 20\n", encoding="utf-8")
        self.job()
        task = asyncio.create_task(self.runner.run_claim(self.runner.claim_next(), asyncio.Event()))
        await asyncio.sleep(0.1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        self.assertEqual(self.record().hook_status, "interrupted")
        self.assertIsNone(self.runner.claim_next())

    async def test_timeout_kills_descendants_that_ignore_termination(self) -> None:
        self.settings.post_promotion_timeout_seconds = 0.1
        self.script.write_text("#!/bin/sh\ntrap '' TERM\nsleep 20 &\nwait\n", encoding="utf-8")
        self.job()
        await asyncio.wait_for(self.runner.run_claim(self.runner.claim_next(), asyncio.Event()), 5)
        self.assertEqual(self.record().hook_status, "failed")
        self.assertIn("timed out", self.record().hook_error)

    async def test_escaped_background_stdout_cannot_block_timeout_cleanup(self) -> None:
        self.settings.post_promotion_timeout_seconds = 0.1
        escaped_pid = self.root / "escaped-child.pid"
        self.script.write_text(
            f"#!{sys.executable}\nimport subprocess, sys, time\nfrom pathlib import Path\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)\n"
            f"Path({str(escaped_pid)!r}).write_text(str(child.pid))\n"
            "print('started background child', flush=True)\ntime.sleep(30)\n",
            encoding="utf-8",
        )
        self.job()
        try:
            with patch("app.post_promotion.PROCESS_TERMINATION_SECONDS", 0.1):
                await asyncio.wait_for(self.runner.run_claim(self.runner.claim_next(), asyncio.Event()), 3)
            job = self.record()
            self.assertEqual(job.hook_status, "failed")
            self.assertIn("timed out", job.hook_error)
            self.assertIn("output incomplete", job.hook_output)
        finally:
            # This fixture intentionally escapes the runner's process group.
            # Kill only the PID recorded by our own disposable child script.
            if escaped_pid.exists():
                try:
                    os.kill(int(escaped_pid.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    async def test_stopped_controller_does_not_claim_or_spawn(self) -> None:
        self.job()
        stop = asyncio.Event()
        stop.set()
        self.assertIsNone(self.runner.claim_next(stop))
        self.assertEqual(self.record().hook_status, "pending")
        claim = self.runner.claim_next()
        with patch("app.post_promotion.asyncio.create_subprocess_exec") as spawn:
            await self.runner.run_claim(claim, stop)
        spawn.assert_not_called()
        self.assertEqual(self.record().hook_status, "interrupted")

    def test_restart_marks_claim_interrupted_and_does_not_replay_it(self) -> None:
        self.job()
        self.assertIsNotNone(self.runner.claim_next())
        restarted = PostPromotionRunner(settings=self.settings, qbt=self.qbt, session_factory=self.sessions)
        self.assertEqual(restarted.recover_interrupted(), 1)
        job = self.record()
        self.assertEqual(job.hook_status, "interrupted")
        self.assertEqual(job.hook_attempts, 1)
        self.assertIn("external effects are unknown", job.hook_error)
        self.assertIsNone(restarted.claim_next())
        self.assertEqual(restarted.recover_interrupted(), 0)

    def test_restart_preserves_pending_work_and_never_discovers_history(self) -> None:
        self.job()
        self.assertEqual(self.runner.recover_interrupted(), 0)
        self.assertEqual(self.record().hook_status, "pending")
        self.assertIsNotNone(self.runner.claim_next())

    async def test_explicit_admin_retry_preserves_attempt_count_and_only_runs_hook(self) -> None:
        from app.main import retry_promotion_hook

        self.job()
        self.update(hook_status="interrupted", hook_attempts=1, hook_error="old interruption", hook_output="old output")
        controller = SimpleNamespace(operation_lock=asyncio.Lock(), require_drained=Mock())
        with patch("app.main.controller", controller), patch("app.main.settings", self.settings), patch("app.main.SessionLocal", self.sessions):
            result = await retry_promotion_hook("hook-job")
        controller.require_drained.assert_called_once()
        self.assertTrue(result["queued"])
        job = self.record()
        self.assertEqual(job.state, "done")
        self.assertEqual(job.hook_status, "pending")
        self.assertEqual(job.hook_attempts, 1)
        self.assertIsNone(job.hook_error)
        self.assertIsNone(job.hook_output)
        self.update(hook_due_at=datetime.utcnow() - timedelta(seconds=1))
        await self.runner.run_claim(self.runner.claim_next(), asyncio.Event())
        self.assertEqual(self.record().hook_status, "succeeded")
        self.assertEqual(self.record().hook_attempts, 2)

    async def test_admin_retry_refuses_active_attempts_and_undrained_controller(self) -> None:
        from fastapi import HTTPException
        from app.main import retry_promotion_hook

        self.job()
        controller = SimpleNamespace(operation_lock=asyncio.Lock(), require_drained=Mock())
        with patch("app.main.controller", controller), patch("app.main.settings", self.settings), patch("app.main.SessionLocal", self.sessions):
            with self.assertRaises(HTTPException) as rejected:
                await retry_promotion_hook("hook-job")
            self.assertEqual(rejected.exception.status_code, 409)
            self.assertEqual(self.record().hook_status, "pending")
            self.update(hook_status="failed")
            controller.require_drained.side_effect = ValueError("Pause and drain first")
            with self.assertRaises(HTTPException) as rejected:
                await retry_promotion_hook("hook-job")
            self.assertEqual(rejected.exception.status_code, 409)
            self.assertEqual(self.record().hook_status, "failed")

    def test_delete_refuses_pending_and_running_hook_without_losing_scan_audit(self) -> None:
        self.job()
        service = JobService()
        service.qbt = Mock()
        for status in ("pending", "running"):
            with self.subTest(status=status):
                self.update(hook_status=status)
                with self.sessions() as db:
                    with self.assertRaises(ValueError):
                        service.delete_job(db, job_id="hook-job")
                self.assertEqual(self.record().hook_status, status)
                with self.sessions() as db:
                    self.assertIsNotNone(db.scalar(select(ScanFile).where(ScanFile.job_id == "hook-job")))
        self.assertEqual(service.qbt.mock_calls, [])

    def test_delete_allows_succeeded_and_failed_hooks_without_changing_qbt(self) -> None:
        service = JobService()
        service.qbt = Mock()
        for status in ("succeeded", "failed"):
            with self.subTest(status=status):
                self.job()
                self.update(hook_status=status)
                with self.sessions() as db:
                    service.delete_job(db, job_id="hook-job")
                with self.sessions() as db:
                    self.assertIsNone(db.get(Job, "hook-job"))
                    self.assertIsNone(db.scalar(select(ScanFile).where(ScanFile.job_id == "hook-job")))
        self.assertEqual(service.qbt.mock_calls, [])

    def test_stale_session_status_cannot_bypass_running_hook_delete_guard(self) -> None:
        self.job()
        self.update(hook_status="failed")
        service = JobService()
        service.qbt = Mock()
        with self.sessions() as stale_db:
            stale_job = stale_db.get(Job, "hook-job")
            self.assertEqual(stale_job.hook_status, "failed")
            self.update(hook_status="running")
            self.assertEqual(stale_job.hook_status, "failed")
            with self.assertRaises(ValueError):
                service.delete_job(stale_db, job_id="hook-job")
        self.assertEqual(self.record().hook_status, "running")
        with self.sessions() as db:
            self.assertIsNotNone(db.scalar(select(ScanFile).where(ScanFile.job_id == "hook-job")))
        self.assertEqual(service.qbt.mock_calls, [])

    def completion_callback(self, db, service, *, qbt_hash="b" * 40):
        return service.ingest_completion_event(
            db, qbt_hash=qbt_hash, qbt_hash_v2=None, unique_tag="ti_job_hook-job", tags=None,
            torrent_name="outdated callback name", content_path=str(self.root / "old-staging" / "payload"),
            root_path=None, save_path=None, size_bytes=42,
        )

    def test_delayed_completion_callback_preserves_pending_hook_source_and_hash(self) -> None:
        self.job()
        service = self.promotion_service()
        with self.sessions() as db:
            returned = self.completion_callback(db, service)
            self.assertEqual(returned.state, "done")
            self.assertEqual(returned.content_path, str(self.source))
            self.assertEqual(returned.qbt_hash, "a" * 40)
            self.assertEqual(returned.hook_status, "pending")
        self.assertIsNotNone(self.runner.claim_next())

    def test_downloading_completion_callback_still_updates_download_hint(self) -> None:
        self.job(queued=False)
        self.update(state="downloading", is_terminal=False, scan_completed_at=None, promoted_at=None)
        service = self.promotion_service()
        with self.sessions() as db:
            returned = self.completion_callback(db, service)
            self.assertEqual(returned.state, "completion_event_received")
            self.assertEqual(returned.qbt_hash, "b" * 40)
            self.assertEqual(returned.content_path, str(self.root / "old-staging" / "payload"))
            self.assertEqual(returned.size_bytes, 42)
            self.assertIsNotNone(returned.completion_event_received_at)
            self.assertIsNone(returned.hook_status)
            self.assertFalse(returned.is_terminal)

    def test_stale_callback_session_cannot_overwrite_concurrent_promotion(self) -> None:
        self.job()
        self.update(state="downloading", is_terminal=False)
        service = self.promotion_service()
        with self.sessions() as stale_db:
            stale_job = stale_db.get(Job, "hook-job")
            self.update(state="done", is_terminal=True)
            self.assertEqual(stale_job.state, "downloading")
            returned = self.completion_callback(stale_db, service, qbt_hash="a" * 40)
            self.assertEqual(returned.state, "done")
            self.assertEqual(returned.content_path, str(self.source))
            self.assertEqual(returned.hook_status, "pending")
        self.assertIsNotNone(self.runner.claim_next())


if __name__ == "__main__":
    unittest.main()
