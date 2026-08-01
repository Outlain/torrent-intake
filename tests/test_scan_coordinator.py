from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Job, ScanFile, ScanRun, ScannerControl
from app.scan_coordinator import ScanCoordinator
from app.scanner import ScannerIdentity


def make_job(job_id: str, state: str = "scanning") -> Job:
    return Job(
        id=job_id,
        magnet_uri="magnet:?xt=urn:btih:" + "a" * 40,
        final_parent="/downloads",
        staging_preference="local",
        staging_root_initial="/staging-local",
        managed_tag="torrent_intake",
        unique_tag=f"ti_job_{job_id}",
        state=state,
    )


def identity(
    *,
    engine: str = "1.5.3",
    database: str = "27901",
    policy: str = "policy-a",
) -> ScannerIdentity:
    return ScannerIdentity(
        backend="clamd",
        engine_version=engine,
        database_version=database,
        database_updated_at=datetime(2026, 7, 29, 8, 0, 0),
        policy_version=policy,
        raw_version=f"ClamAV {engine}/{database}/Wed Jul 29 08:00:00 2026",
    )


class ScanCheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.coordinator = ScanCoordinator()

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_definition_update_preserves_checkpoint_but_engine_update_resets_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory, Session(self.engine) as db:
            path = Path(directory) / "payload.bin"
            path.write_bytes(b"safe test data")
            job = make_job("job-checkpoint")
            run = ScanRun(job_id=job.id, worker_id="worker")
            db.add_all([job, run])
            db.commit()

            self.coordinator._prepare_manifest(db, job, run, directory, identity())
            scan_file = db.scalar(select(ScanFile).where(ScanFile.job_id == job.id))
            self.assertIsNotNone(scan_file)
            scan_file.status = "clean"
            scan_file.scanned_at = datetime.utcnow()
            run.completed_files = 1
            run.completed_bytes = scan_file.size_bytes
            db.commit()

            self.coordinator._prepare_manifest(
                db,
                job,
                run,
                directory,
                identity(database="27902"),
            )
            db.refresh(scan_file)
            self.assertEqual(scan_file.status, "clean")

            scan_file.status = "error"
            db.commit()
            self.coordinator._prepare_manifest(
                db,
                job,
                run,
                directory,
                identity(database="27902"),
            )
            db.refresh(scan_file)
            self.assertEqual(scan_file.status, "pending")

            scan_file.status = "clean"
            db.commit()
            self.coordinator._prepare_manifest(
                db,
                job,
                run,
                directory,
                identity(engine="1.5.4", database="27903"),
            )
            db.refresh(scan_file)
            self.assertEqual(scan_file.status, "pending")
            self.assertEqual(run.completed_files, 0)

    def test_policy_update_resets_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory, Session(self.engine) as db:
            (Path(directory) / "payload.bin").write_bytes(b"safe test data")
            job = make_job("job-policy")
            run = ScanRun(job_id=job.id, worker_id="worker")
            db.add_all([job, run])
            db.commit()
            self.coordinator._prepare_manifest(db, job, run, directory, identity())
            scan_file = db.scalar(select(ScanFile).where(ScanFile.job_id == job.id))
            scan_file.status = "clean"
            scan_file.scanned_at = datetime.utcnow()
            db.commit()

            self.coordinator._prepare_manifest(
                db,
                job,
                run,
                directory,
                identity(policy="policy-b"),
            )
            db.refresh(scan_file)
            self.assertEqual(scan_file.status, "pending")

    def test_prioritizing_paused_scan_does_not_resume_it(self) -> None:
        with Session(self.engine) as db:
            job = make_job("job-paused", state="scan_paused")
            run = ScanRun(job_id=job.id, pause_requested=True)
            db.add_all([job, run])
            db.commit()

            with self.assertRaisesRegex(ValueError, "resume paused scans explicitly"):
                self.coordinator._prioritize_job(db, job.id)

            db.refresh(job)
            db.refresh(run)
            self.assertEqual(job.state, "scan_paused")
            self.assertTrue(run.pause_requested)

    def test_eta_waits_for_three_samples(self) -> None:
        with Session(self.engine) as db:
            job = make_job("job-eta")
            run = ScanRun(
                job_id=job.id,
                total_files=4,
                completed_files=2,
                total_bytes=40 * 1024 * 1024,
                completed_bytes=20 * 1024 * 1024,
            )
            db.add_all([job, run])
            for index in range(2):
                db.add(
                    ScanFile(
                        job_id=job.id,
                        relative_path=f"done-{index}",
                        size_bytes=10 * 1024 * 1024,
                        mtime_ns=index,
                        status="clean",
                        scanned_at=datetime.utcnow(),
                        scan_duration_seconds=10 + index,
                    )
                )
            db.add(
                ScanFile(
                    job_id=job.id,
                    relative_path="pending",
                    size_bytes=20 * 1024 * 1024,
                    mtime_ns=3,
                    status="pending",
                )
            )
            db.commit()

            self.assertEqual(self.coordinator._estimate_eta(db, run), (None, None))
            self.assertEqual(self.coordinator._progress_percent(run), 50.0)

    def test_forced_restart_retries_only_in_progress_file(self) -> None:
        with Session(self.engine) as db:
            job = make_job("job-restart")
            run = ScanRun(
                job_id=job.id,
                worker_id="old-container",
                current_file="active.bin",
                current_file_started_at=datetime.utcnow(),
                completed_files=1,
                completed_bytes=10,
                failure_count=0,
            )
            clean_file = ScanFile(
                job_id=job.id,
                relative_path="clean.bin",
                size_bytes=10,
                mtime_ns=1,
                status="clean",
                scan_duration_seconds=2,
            )
            active_file = ScanFile(
                job_id=job.id,
                relative_path="active.bin",
                size_bytes=20,
                mtime_ns=2,
                status="scanning",
                scan_started_at=datetime.utcnow(),
            )
            db.add_all([job, run, clean_file, active_file])
            db.commit()

            self.coordinator.recover_interrupted_scans(db, force=True)

            db.refresh(job)
            db.refresh(run)
            db.refresh(clean_file)
            db.refresh(active_file)
            self.assertEqual(job.state, "scan_pending")
            self.assertEqual(clean_file.status, "clean")
            self.assertEqual(active_file.status, "pending")
            self.assertIsNone(active_file.scan_started_at)
            self.assertIsNone(run.current_file)
            self.assertEqual(run.failure_count, 0)

    def test_maintenance_blocks_new_claims(self) -> None:
        with Session(self.engine) as db:
            job = make_job("job-maintenance", state="scan_pending")
            run = ScanRun(job_id=job.id, queued_at=datetime.utcnow())
            control = ScannerControl(
                id=1,
                requested_slots=2,
                maintenance_mode=True,
                maintenance_reason="engine update",
            )
            db.add_all([job, run, control])
            db.commit()

            claims = self.coordinator.claim_jobs(db, "scheduler")

            self.assertEqual(claims, [])
            db.refresh(job)
            self.assertEqual(job.state, "scan_pending")


if __name__ == "__main__":
    unittest.main()
