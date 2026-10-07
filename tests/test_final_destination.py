from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import Response

from app.config import Settings
from app.db import Base
from app.models import FINAL_DESTINATION_EDITABLE_STATES, FINAL_DESTINATION_LOCK_MARKERS, Job, ScanFile, ScanRun
from app.scan_coordinator import ScanCoordinator
from app.schemas import JobFinalDestinationUpdate, JobOut
from app.service import FinalDestinationConflict, JobService


class FinalDestinationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.library = self.root / "library"
        self.library.mkdir()
        self.settings = Settings(
            _env_file=None, final_parent_prefix=str(self.library),
            local_staging_root=str(self.root / "local"),
            nas_staging_root=str(self.library / "staging"),
        )
        self.engine = create_engine(f"sqlite:///{self.root / 'jobs.db'}")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.service = JobService.__new__(JobService)
        self.service.settings = self.settings
        self.service.qbt = MagicMock()
        self.service.logger = logging.getLogger(__name__)
        self.coordinator = ScanCoordinator.__new__(ScanCoordinator)
        self.coordinator.settings = self.settings
        self.service.scan_coordinator = self.coordinator

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.temporary.cleanup()

    def job(self, job_id="example", *, state="downloading", staging="local"):
        path = self.settings.local_staging_root if staging == "local" else self.settings.nas_staging_root
        job = Job(
            id=job_id, magnet_uri="magnet:?xt=urn:btih:" + "a" * 40,
            final_parent=str(self.library / "Old"), final_category="Movies",
            staging_preference=staging, staging_actual=staging,
            staging_root_initial=path, staging_root_actual=path,
            managed_tag="torrent_intake", unique_tag=f"ti_job_{job_id}",
            qbt_hash="a" * 40, state=state, nas_staging_id="primary",
            nas_staging_path=self.settings.nas_staging_root,
        )
        self.db.add(job)
        self.db.commit()
        return job

    def edit(self, job, *, destination=None, expected=None):
        return self.service.update_final_destination(
            self.db, job_id=job.id,
            final_parent=destination or str(self.library / "New"),
            expected_final_parent=expected or job.final_parent,
        )

    def paused_job(self, job_id="paused", *, staging="local", all_files_complete=False):
        job = self.job(job_id, state="scan_paused", staging=staging)
        now = datetime.utcnow()
        job.completion_event_received_at = now
        job.download_complete_at = now
        job.content_path = str(Path(job.staging_root_actual) / job_id)
        job.custom_tags = ["Keep me"]
        run = ScanRun(
            job_id=job.id, pause_requested=True, priority=100, attempts=2,
            queued_at=now, started_at=now, root_path=job.content_path,
            total_files=2, completed_files=2 if all_files_complete else 1,
            total_bytes=20, completed_bytes=20 if all_files_complete else 10,
            scanner_version="ClamAV test", engine_version="test-engine",
            database_version="test-definitions", database_updated_at=now,
            policy_version="test-policy",
        )
        self.db.add(run)
        for index in range(2):
            clean = index == 0 or all_files_complete
            self.db.add(ScanFile(
                job_id=job.id, relative_path=f"file-{index}.mkv", size_bytes=10,
                mtime_ns=123, ctime_ns=456, device=7, inode=index + 1,
                status="clean" if clean else "pending", attempts=1 if clean else 0,
                engine_version="test-engine", database_version="test-definitions",
                policy_version="test-policy", scan_method="stream" if clean else None,
                scanned_at=now if clean else None,
            ))
        self.db.commit()
        return job, run

    @staticmethod
    def snapshot(row):
        return {column.name: getattr(row, column.name) for column in row.__table__.columns}

    def assert_editable_flag(self, job, expected):
        run = self.db.get(ScanRun, job.id)
        self.assertEqual(job.final_destination_is_editable(scan_run=run), expected)
        self.coordinator.enrich_jobs(self.db, [job])
        self.assertEqual(JobOut.model_validate(job).can_edit_final_destination, expected)

    def test_local_and_nas_downloads_change_only_destination_and_timestamp(self):
        for staging in ("local", "nas"):
            with self.subTest(staging=staging):
                job = self.job(staging, staging=staging)
                before = {column.name: getattr(job, column.name) for column in Job.__table__.columns}
                result = self.edit(job)
                after = {column.name: getattr(result, column.name) for column in Job.__table__.columns}
                self.assertEqual(after.pop("final_parent"), str(self.library / "New"))
                self.assertGreaterEqual(after.pop("updated_at"), before.pop("updated_at"))
                before.pop("final_parent")
                self.assertEqual(after, before)
                self.assertTrue(result.can_edit_final_destination)
                self.assertEqual(list(self.db.scalars(select(ScanRun))), [])
        self.service.qbt.assert_not_called()
        self.assertEqual(self.service.qbt.mock_calls, [])
        self.assertFalse((self.library / "New").exists(), "Planning a move must not create any directory")

    def test_result_survives_fresh_session(self):
        job = self.job()
        result = self.edit(job)
        with Session(self.engine) as db:
            restored = db.get(Job, job.id)
            self.assertEqual(restored.final_parent, result.final_parent)
            self.assertEqual(restored.staging_root_actual, self.settings.local_staging_root)

    def test_paused_local_nas_and_last_file_edits_preserve_all_scan_checkpoints(self):
        for staging in ("local", "nas"):
            for all_files_complete in (False, True):
                with self.subTest(staging=staging, all_files_complete=all_files_complete):
                    job, run = self.paused_job(
                        f"{staging}-{all_files_complete}", staging=staging,
                        all_files_complete=all_files_complete,
                    )
                    before_job = self.snapshot(job)
                    before_run = self.snapshot(run)
                    files = list(self.db.scalars(select(ScanFile).where(ScanFile.job_id == job.id)))
                    before_files = [self.snapshot(row) for row in files]
                    self.assert_editable_flag(job, True)
                    result = self.edit(job)
                    self.assertTrue(result.can_edit_final_destination)
                    with Session(self.engine) as db:
                        restored = db.get(Job, job.id)
                        after_job = self.snapshot(restored)
                        self.assertEqual(after_job.pop("final_parent"), str(self.library / "New"))
                        self.assertGreaterEqual(after_job.pop("updated_at"), before_job.pop("updated_at"))
                        before_job.pop("final_parent")
                        self.assertEqual(after_job, before_job)
                        self.assertEqual(self.snapshot(db.get(ScanRun, job.id)), before_run)
                        after_files = list(db.scalars(select(ScanFile).where(ScanFile.job_id == job.id)))
                        self.assertEqual([self.snapshot(row) for row in after_files], before_files)
        self.assertEqual(self.service.qbt.mock_calls, [])
        self.assertFalse((self.library / "New").exists())

    def test_all_pre_completion_states_allow_editing(self):
        for state in FINAL_DESTINATION_EDITABLE_STATES:
            with self.subTest(state=state):
                job = self.job(state, state=state)
                self.assertEqual(self.edit(job).state, state)

    def test_completed_failed_and_transient_states_reject_editing(self):
        for state in (
            "submitted", "adding_to_qbt", "retrying", "completion_event_received", "download_complete",
            "scan_pending", "scan_paused", "scanning", "scan_clean", "scan_error", "promoting",
            "done", "infected_held", "infected_quarantined", "infected_deleted", "error",
        ):
            with self.subTest(state=state):
                job = self.job(state, state=state)
                old = job.final_parent
                with self.assertRaises(FinalDestinationConflict):
                    self.edit(job)
                self.assertEqual(job.final_parent, old)
                self.assertFalse(job.final_destination_is_editable(scan_run=None))

    def test_completion_infection_and_hook_markers_reject_stale_downloading_state(self):
        for marker in (*FINAL_DESTINATION_LOCK_MARKERS, "is_terminal"):
            with self.subTest(marker=marker):
                job = self.job(marker)
                value = datetime.utcnow() if marker.endswith("_at") else "present"
                setattr(job, marker, True if marker == "is_terminal" else value)
                self.db.commit()
                with self.assertRaises(FinalDestinationConflict):
                    self.edit(job)
                self.assertFalse(job.final_destination_is_editable(scan_run=None))

    def test_paused_edit_requires_an_existing_run(self):
        job = self.job(state="scan_paused")
        self.assert_editable_flag(job, False)
        with self.assertRaises(FinalDestinationConflict):
            self.edit(job)

    def test_paused_edit_rejects_every_unsafe_scan_run_field(self):
        unsafe_fields = (
            ("pause_requested", False), ("worker_id", "worker-1"), ("worker_id", ""),
            ("lease_expires_at", datetime(2000, 1, 1)),
            ("heartbeat_at", datetime(2000, 1, 1)),
            ("current_file", "file.mkv"), ("current_file", ""),
            ("current_file_started_at", datetime(2000, 1, 1)),
            ("verdict", "clean"), ("verdict", "infected"), ("verdict", "error"),
            ("verdict", ""),
        )
        for index, (field, value) in enumerate(unsafe_fields):
            with self.subTest(field=field, value=value):
                job, run = self.paused_job(f"unsafe-run-{index}")
                setattr(run, field, value)
                self.db.commit()
                before = self.snapshot(job)
                self.assert_editable_flag(job, False)
                with self.assertRaises(FinalDestinationConflict):
                    self.edit(job)
                self.assertEqual(self.snapshot(job), before)

    def test_paused_edit_rejects_completed_infected_terminal_and_hook_markers(self):
        for marker in (
            "scan_completed_at", "promoted_at", "deleted_at", "threat_name",
            "quarantine_path", "hook_status", "is_terminal",
        ):
            with self.subTest(marker=marker):
                job, _run = self.paused_job(marker)
                value = datetime.utcnow() if marker.endswith("_at") else "present"
                setattr(job, marker, True if marker == "is_terminal" else value)
                self.db.commit()
                self.assert_editable_flag(job, False)
                with self.assertRaises(FinalDestinationConflict):
                    self.edit(job)
                self.assertEqual(job.final_parent, str(self.library / "Old"))

    def test_paused_run_does_not_make_other_job_states_editable(self):
        for state in (
            *FINAL_DESTINATION_EDITABLE_STATES, "submitted", "adding_to_qbt", "retrying",
            "completion_event_received", "download_complete", "scan_pending", "scanning",
            "scan_clean", "scan_infected", "scan_error", "promoting", "done",
            "quarantining_infected", "deleting_infected", "infected_held",
            "infected_quarantined", "infected_deleted", "error",
        ):
            with self.subTest(state=state):
                job, _run = self.paused_job(state)
                job.state = state
                self.db.commit()
                self.assert_editable_flag(job, False)
                with self.assertRaises(FinalDestinationConflict):
                    self.edit(job)

    def test_scan_history_rejects_edit_even_if_retry_returned_to_downloading(self):
        job = self.job()
        self.db.add(ScanRun(job_id=job.id, attempts=1))
        self.db.commit()
        self.coordinator.enrich_jobs(self.db, [job])
        self.assertFalse(JobOut.model_validate(job).can_edit_final_destination)
        with self.assertRaises(FinalDestinationConflict):
            self.edit(job)

    def test_api_flag_uses_same_eligibility_in_enriched_job(self):
        job = self.job()
        self.coordinator.enrich_jobs(self.db, [job])
        self.assertTrue(JobOut.model_validate(job).can_edit_final_destination)
        job.download_complete_at = datetime.utcnow()
        self.db.commit()
        self.coordinator.enrich_jobs(self.db, [job])
        self.assertFalse(JobOut.model_validate(job).can_edit_final_destination)

    def test_invalid_destinations_do_not_touch_job_or_qbt(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.library / "escape").symlink_to(outside, target_is_directory=True)
        paused, _run = self.paused_job()
        for job in (self.job(), paused):
            before = self.snapshot(job)
            for destination in (
                "relative", "/etc", str(self.library) + "-other/Film", str(self.library / "../outside"),
                str(self.library / "staging/Film"), str(self.library / "escape/Film"),
                str(self.library / "line\nbreak"), str(self.root / "local/Film"), "/copy-target/Film",
            ):
                with self.subTest(state=job.state, destination=destination), self.assertRaises(ValueError):
                    self.edit(job, destination=destination)
                self.assertEqual(self.snapshot(job), before)
        self.assertEqual(self.service.qbt.mock_calls, [])

    def test_destination_is_canonicalized(self):
        job = self.job()
        result = self.edit(job, destination=str(self.library / "Library/../Movies") + "/")
        self.assertEqual(result.final_parent, str(self.library / "Movies"))

    def test_stale_editor_cannot_overwrite_another_edit(self):
        paused, _run = self.paused_job()
        for job in (self.job(), paused):
            with self.subTest(state=job.state):
                original = job.final_parent
                self.edit(job, destination=str(self.library / "First choice"))
                with self.assertRaises(FinalDestinationConflict):
                    self.edit(job, expected=original, destination=str(self.library / "Second choice"))
                self.assertEqual(job.final_parent, str(self.library / "First choice"))

    def test_queue_wins_race_before_atomic_update(self):
        job = self.job()
        original = job.final_parent
        execute = self.db.execute
        raced = False

        def execute_after_queue(statement, *args, **kwargs):
            nonlocal raced
            if not raced:
                raced = True
                with Session(self.engine) as worker_db:
                    worker_job = worker_db.get(Job, job.id)
                    self.coordinator.queue_job(worker_db, worker_job)
                    worker_db.commit()
            return execute(statement, *args, **kwargs)

        with patch.object(self.db, "execute", side_effect=execute_after_queue):
            with self.assertRaises(FinalDestinationConflict):
                self.edit(job, expected=original)
        self.assertEqual(job.final_parent, original)
        self.assertEqual(job.state, "scan_pending")

    def test_edit_wins_race_and_stale_worker_does_not_overwrite_destination(self):
        job = self.job()
        with Session(self.engine) as worker_db:
            worker_job = worker_db.get(Job, job.id)
            original = worker_job.final_parent
            edited = self.edit(job).final_parent
            self.assertEqual(worker_job.final_parent, original)
            self.coordinator.queue_job(worker_db, worker_job)
            worker_db.commit()
        with Session(self.engine) as db:
            refreshed = db.get(Job, job.id)
            self.assertEqual(refreshed.final_parent, edited)
            self.assertEqual(refreshed.state, "scan_pending")

    def test_resume_wins_race_before_atomic_paused_update(self):
        job, _run = self.paused_job()
        original = job.final_parent
        execute = self.db.execute
        raced = False

        def execute_after_resume(statement, *args, **kwargs):
            nonlocal raced
            if not raced and getattr(statement, "is_update", False):
                raced = True
                with Session(self.engine) as worker_db:
                    result = self.coordinator.resume_jobs(worker_db, [job.id])
                    self.assertEqual(result["processed"], 1)
            return execute(statement, *args, **kwargs)

        with patch.object(self.db, "execute", side_effect=execute_after_resume):
            with self.assertRaises(FinalDestinationConflict):
                self.edit(job, expected=original)
        self.assertTrue(raced)
        self.assertEqual(job.final_parent, original)
        self.assertEqual(job.state, "scan_pending")
        self.assertFalse(self.db.get(ScanRun, job.id).pause_requested)

    def test_paused_atomic_update_rechecks_run_even_when_job_state_is_unchanged(self):
        for field, value in (("worker_id", "new-worker"), ("pause_requested", False), ("verdict", "clean")):
            with self.subTest(field=field):
                job, _run = self.paused_job(f"race-{field}")
                original = job.final_parent
                execute = self.db.execute
                raced = False

                def execute_after_run_change(statement, *args, **kwargs):
                    nonlocal raced
                    if not raced and getattr(statement, "is_update", False):
                        raced = True
                        with Session(self.engine) as worker_db:
                            worker_run = worker_db.get(ScanRun, job.id)
                            setattr(worker_run, field, value)
                            worker_db.commit()
                    return execute(statement, *args, **kwargs)

                with patch.object(self.db, "execute", side_effect=execute_after_run_change):
                    with self.assertRaises(FinalDestinationConflict):
                        self.edit(job, expected=original)
                self.assertTrue(raced)
                self.assertEqual(job.final_parent, original)
                self.assertEqual(job.state, "scan_paused")

    def test_paused_edit_survives_resume_by_worker_with_stale_destination(self):
        job, _run = self.paused_job()
        with Session(self.engine) as worker_db:
            worker_job = worker_db.get(Job, job.id)
            original = worker_job.final_parent
            edited = self.edit(job).final_parent
            self.assertEqual(worker_job.final_parent, original)
            result = self.coordinator.resume_jobs(worker_db, [job.id])
            self.assertEqual(result["processed"], 1)
        with Session(self.engine) as db:
            refreshed = db.get(Job, job.id)
            self.assertEqual(refreshed.final_parent, edited)
            self.assertEqual(refreshed.state, "scan_pending")
            self.assertFalse(db.get(ScanRun, job.id).pause_requested)

    def test_api_get_list_and_patch_agree_on_paused_edit_eligibility(self):
        from app import main

        async def request(method, path, payload=None):
            messages = []
            body_sent = False
            body = json.dumps(payload).encode() if payload is not None else b""

            async def receive():
                nonlocal body_sent
                if not body_sent:
                    body_sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                await asyncio.Event().wait()

            async def send(message):
                messages.append(message)

            await asyncio.wait_for(main.app({
                "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                "http_version": "1.1", "method": method, "scheme": "http",
                "path": path, "raw_path": path.encode(), "query_string": b"",
                "root_path": "", "headers": [(b"content-type", b"application/json")],
                "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
            }, receive, send), timeout=5)
            response = next(item for item in messages if item["type"] == "http.response.start")
            data = b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")
            return response["status"], json.loads(data)

        def database():
            with Session(self.engine) as db:
                yield db

        cases = [(self.job("api-download"), True)]
        for label, field, value in (
            ("paused", None, None), ("last-file", None, None),
            ("worker", "worker_id", "worker"), ("active-file", "current_file", "file.mkv"),
            ("clean-verdict", "verdict", "clean"), ("not-paused", "pause_requested", False),
        ):
            job, run = self.paused_job(f"api-{label}", all_files_complete=label == "last-file")
            if field:
                setattr(run, field, value)
            cases.append((job, field is None))
        cases.append((self.job("api-no-run", state="scan_paused"), False))
        job, _run = self.paused_job("api-completed")
        job.scan_completed_at = datetime.utcnow()
        cases.append((job, False))
        job, _run = self.paused_job("api-still-scanning")
        job.state = "scanning"
        cases.append((job, False))
        self.db.commit()

        with patch.object(main, "service", self.service), patch.object(main, "controller", None), \
                patch.object(self.service, "enrich_jobs_with_live_stats", side_effect=lambda jobs: jobs), \
                patch.dict(main.app.dependency_overrides, {main.get_db: database}):
            status, listed = asyncio.run(request("GET", "/jobs"))
            self.assertEqual(status, 200)
            flags = {row["id"]: row["can_edit_final_destination"] for row in listed}
            for job, allowed in cases:
                with self.subTest(job_id=job.id):
                    self.assertEqual(flags[job.id], allowed)
                    status, detail = asyncio.run(request("GET", f"/jobs/{job.id}"))
                    self.assertEqual(status, 200)
                    self.assertEqual(detail["can_edit_final_destination"], allowed)
                    status, updated = asyncio.run(request("PATCH", f"/jobs/{job.id}/final-destination", {
                        "final_parent": str(self.library / "New"), "expected_final_parent": job.final_parent,
                    }))
                    self.assertEqual(status, 200 if allowed else 409, updated)
                    if allowed:
                        self.assertEqual(updated["final_parent"], str(self.library / "New"))
                        self.assertTrue(updated["can_edit_final_destination"])
                        self.assertEqual(updated["state"], job.state)
        self.assertEqual(self.service.qbt.mock_calls, [])

    def test_missing_job_is_not_created(self):
        with self.assertRaises(LookupError):
            self.service.update_final_destination(
                self.db, job_id="missing", final_parent=str(self.library / "New"),
                expected_final_parent=str(self.library / "Old"),
            )
        self.assertEqual(list(self.db.scalars(select(Job))), [])

    def test_route_forwards_payload_and_returns_specific_status_codes(self):
        from app import main
        payload = JobFinalDestinationUpdate(final_parent="/downloads/New", expected_final_parent="/downloads/Old")
        with patch("app.main.service") as service:
            service.update_final_destination.return_value = "updated"
            self.assertEqual(main.update_final_destination("job", payload, self.db), "updated")
            service.update_final_destination.assert_called_once_with(self.db, job_id="job", **payload.model_dump())
            for error, status_code in ((LookupError("missing"), 404),
                                       (FinalDestinationConflict("changed"), 409),
                                       (ValueError("invalid path"), 422)):
                service.update_final_destination.side_effect = error
                with self.assertRaises(HTTPException) as raised:
                    main.update_final_destination("job", payload, self.db)
                self.assertEqual(raised.exception.status_code, status_code)


class FinalDestinationGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_paused_controller_blocks_patch_before_handler(self):
        from app import main
        request = Request({"type": "http", "method": "PATCH", "scheme": "http",
                           "path": "/jobs/example/final-destination", "query_string": b"", "headers": []})
        called = []

        async def handler(_request):
            called.append(True)
            return Response("unexpected")

        controller = SimpleNamespace(paused=True, active_mutations=0)
        with patch("app.main.controller", controller):
            response = await main.administration_guard(request, handler)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(called, [])
        self.assertEqual(controller.active_mutations, 0)


if __name__ == "__main__":
    unittest.main()
