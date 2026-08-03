from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Job
from app.paths import canonical_final_parent
from app.torrent_guard import TorrentSafetyGuard


def settings(**changes):
    value = SimpleNamespace(
        allowed_final_parent_prefixes=["/downloads"],
        local_staging_root="/staging-local",
        nas_staging_root="/downloads/torrent-intake/staging",
    )
    for key, item in changes.items():
        setattr(value, key, item)
    return value


class DestinationPathTests(unittest.TestCase):
    def test_rejects_traversal_and_operational_paths(self) -> None:
        for value in ("/downloads/../app", "/downloads/docker", "/downloads/docker/clamav"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                canonical_final_parent(value, settings())

    def test_accepts_normal_media_destination(self) -> None:
        self.assertEqual(
            canonical_final_parent("/downloads/Movies", settings()),
            "/downloads/Movies",
        )

    def test_rejects_existing_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloads = root / "downloads"
            outside = root / "app"
            downloads.mkdir()
            outside.mkdir()
            (downloads / "escape").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                canonical_final_parent(
                    str(downloads / "escape"),
                    settings(allowed_final_parent_prefixes=[str(downloads)]),
                )


class TorrentGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_requires_tags_completion_pause_and_staging_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory, Session(self.engine) as db:
            staging = Path(directory) / "staging"
            content = staging / "torrent"
            content.mkdir(parents=True)
            job = Job(
                id="guard-job",
                magnet_uri="magnet:?xt=urn:btih:" + "a" * 40,
                final_parent="/downloads/Movies",
                staging_preference="local",
                staging_root_initial=str(staging),
                staging_root_actual=str(staging),
                managed_tag="torrent_intake",
                unique_tag="ti_job_guard",
                qbt_hash="abc",
                state="scanning",
            )
            db.add(job)
            db.commit()
            torrent = SimpleNamespace(
                hash="abc",
                tags="torrent_intake, ti_job_guard",
                progress=1.0,
                amount_left=0,
                state="pausedUP",
                save_path=str(staging),
                content_path=str(content),
            )

            self.assertEqual(
                TorrentSafetyGuard().validate_staging(db, job, torrent, require_paused=True),
                content,
            )
            torrent.tags = "torrent_intake"
            with self.assertRaisesRegex(RuntimeError, "tags are missing"):
                TorrentSafetyGuard().validate_staging(db, job, torrent, require_paused=True)

            torrent.tags = "torrent_intake, ti_job_guard"
            torrent.state = "uploading"
            with self.assertRaisesRegex(RuntimeError, "not paused"):
                TorrentSafetyGuard().validate_staging(db, job, torrent, require_paused=True)

            torrent.state = "pausedUP"
            torrent.progress = 0.99
            with self.assertRaisesRegex(RuntimeError, "not complete"):
                TorrentSafetyGuard().validate_staging(db, job, torrent, require_paused=True)

            torrent.progress = 1.0
            outside = Path(directory) / "outside"
            outside.mkdir()
            torrent.save_path = str(outside)
            with self.assertRaisesRegex(RuntimeError, "escaped expected staging"):
                TorrentSafetyGuard().validate_staging(db, job, torrent, require_paused=True)

    def test_rejects_hash_reuse_by_another_active_job(self) -> None:
        with Session(self.engine) as db:
            jobs = [
                Job(
                    id=f"job-{index}",
                    magnet_uri="magnet:?xt=urn:btih:" + str(index) * 40,
                    final_parent="/downloads/Movies",
                    staging_preference="local",
                    staging_root_initial="/staging-local",
                    managed_tag="torrent_intake",
                    unique_tag=f"ti_job_{index}",
                    qbt_hash="abc",
                    state="scanning",
                )
                for index in (1, 2)
            ]
            db.add_all(jobs)
            db.commit()
            torrent = SimpleNamespace(
                hash="ABC",
                tags="torrent_intake, ti_job_1",
                progress=1.0,
                amount_left=0,
                state="pausedUP",
            )
            with self.assertRaisesRegex(RuntimeError, "another active"):
                TorrentSafetyGuard().validate_common(
                    db, jobs[0], torrent, require_paused=True
                )

    def test_quarantine_requires_save_and_content_inside_exact_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory, Session(self.engine) as db:
            root = Path(directory)
            destination = root / "quarantine" / "job"
            content = destination / "payload"
            content.mkdir(parents=True)
            job = Job(
                id="quarantine-job",
                magnet_uri="magnet:?xt=urn:btih:" + "b" * 40,
                final_parent="/downloads/Movies",
                staging_preference="local",
                staging_root_initial="/staging-local",
                managed_tag="torrent_intake",
                unique_tag="ti_job_quarantine",
                qbt_hash="def",
                state="quarantining_infected",
            )
            db.add(job)
            db.commit()
            torrent = SimpleNamespace(
                hash="def",
                tags="torrent_intake, ti_job_quarantine",
                progress=1.0,
                amount_left=0,
                state="pausedUP",
                save_path=str(destination),
                content_path=str(content),
            )
            self.assertEqual(
                TorrentSafetyGuard().validate_quarantine_destination(
                    db, job, torrent, destination
                ),
                content,
            )
            torrent.content_path = str(root)
            with self.assertRaisesRegex(RuntimeError, "escaped expected staging"):
                TorrentSafetyGuard().validate_quarantine_destination(
                    db, job, torrent, destination
                )

    def test_promotion_requires_content_inside_exact_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory, Session(self.engine) as db:
            root = Path(directory)
            destination = root / "downloads" / "Movies"
            content = destination / "payload"
            outside = root / "outside"
            content.mkdir(parents=True)
            outside.mkdir()
            job = Job(
                id="promotion-job",
                magnet_uri="magnet:?xt=urn:btih:" + "c" * 40,
                final_parent=str(destination),
                staging_preference="local",
                staging_root_initial="/staging-local",
                managed_tag="torrent_intake",
                unique_tag="ti_job_promotion",
                qbt_hash="ghi",
                state="promoting",
            )
            db.add(job)
            db.commit()
            torrent = SimpleNamespace(
                hash="ghi",
                tags="torrent_intake, ti_job_promotion",
                progress=1.0,
                amount_left=0,
                state="pausedUP",
                save_path=str(destination),
                content_path=str(content),
            )
            self.assertEqual(
                TorrentSafetyGuard().validate_destination(db, job, torrent),
                content,
            )
            torrent.content_path = str(outside)
            with self.assertRaisesRegex(RuntimeError, "escaped expected staging"):
                TorrentSafetyGuard().validate_destination(db, job, torrent)


if __name__ == "__main__":
    unittest.main()
