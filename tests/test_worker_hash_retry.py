from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.worker import QBT_HASH_RETRY_INTERVAL_SECONDS, _run_qbt_hash_retry_cycle


class _FakeDb:
    def __init__(self, jobs):
        self.jobs = jobs
        self.rollback_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def scalars(self, _statement):
        return self.jobs

    def rollback(self):
        self.rollback_calls += 1


class _FakeService:
    def __init__(self, fail_job_id: str | None = None):
        self.fail_job_id = fail_job_id
        self.calls: list[str] = []

    def _resolve_hash_for_job(self, _db, job):
        self.calls.append(job.id)
        if job.id == self.fail_job_id:
            raise RuntimeError("temporary qB lookup failure")


class QbtHashRetryWorkerTests(unittest.TestCase):
    def test_retry_interval_is_fast_and_separate_from_management_poll(self) -> None:
        self.assertEqual(QBT_HASH_RETRY_INTERVAL_SECONDS, 5)

    def test_cycle_retries_every_waiting_job(self) -> None:
        jobs = [SimpleNamespace(id="job-a"), SimpleNamespace(id="job-b")]
        db = _FakeDb(jobs)
        service = _FakeService()

        with patch("app.worker.SessionLocal", return_value=db):
            processed = _run_qbt_hash_retry_cycle(service)

        self.assertEqual(processed, 2)
        self.assertEqual(service.calls, ["job-a", "job-b"])
        self.assertEqual(db.rollback_calls, 0)

    def test_one_temporary_failure_does_not_block_other_waiting_jobs(self) -> None:
        jobs = [SimpleNamespace(id="job-a"), SimpleNamespace(id="job-b")]
        db = _FakeDb(jobs)
        service = _FakeService(fail_job_id="job-a")

        with patch("app.worker.SessionLocal", return_value=db):
            processed = _run_qbt_hash_retry_cycle(service)

        self.assertEqual(processed, 2)
        self.assertEqual(service.calls, ["job-a", "job-b"])
        self.assertEqual(db.rollback_calls, 1)


if __name__ == "__main__":
    unittest.main()
