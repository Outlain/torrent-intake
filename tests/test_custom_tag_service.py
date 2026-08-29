from __future__ import annotations

import logging
import unittest
from unittest.mock import MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base
from app.models import Job
from app.schemas import JobOut
from app.service import JobService


VALID_MAGNET = f"magnet:?xt=urn:btih:{'b' * 40}"


class CustomTagServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)

        self.service = JobService.__new__(JobService)
        self.service.settings = Settings(
            managed_tag="torrent_intake",
            local_staging_root="/staging-local",
            nas_staging_root="/downloads/torrent-intake/staging",
            final_parent_prefix="/downloads",
        )
        self.service.qbt = MagicMock()
        self.service.qbt.find_existing_from_magnet.return_value = None
        self.service.qbt.list_torrents.return_value = []
        self.service.scan_coordinator = MagicMock()
        self.service.logger = logging.getLogger(__name__)
        self.service._resolve_hash_for_job = MagicMock()
        self.service._evaluate_staging_now = MagicMock()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def submit(self, custom_tags: list[str]) -> Job:
        return self.service.submit_job(
            self.db,
            magnet_uri=VALID_MAGNET,
            final_parent="/downloads/Movies",
            final_category="Movies",
            staging_preference="local",
            custom_tags=custom_tags,
        )

    def test_submit_persists_and_adds_custom_tags_after_private_tags(self) -> None:
        job = self.submit(["Review", "Needs subtitles"])

        self.assertEqual(job.custom_tags, ["Review", "Needs subtitles"])
        call = self.service.qbt.add_torrent.call_args.kwargs
        self.assertEqual(
            call["tags"],
            ["torrent_intake", job.unique_tag, "Review", "Needs subtitles"],
        )
        self.assertEqual(JobOut.model_validate(job).custom_tags, ["Review", "Needs subtitles"])

    def test_retry_recreates_missing_torrent_with_persisted_tags(self) -> None:
        job = self.submit(["Review", "Long term"])
        job_id = job.id
        unique_tag = job.unique_tag
        self.service.qbt.add_torrent.reset_mock()
        job.state = "error"
        job.qbt_hash = None
        self.db.add(job)
        self.db.commit()

        # A fresh Session simulates an application/container restart and proves
        # the retry uses the durable JSON column rather than in-memory state.
        self.db.close()
        self.db = Session(self.engine)
        self.assertEqual(self.db.get(Job, job_id).custom_tags, ["Review", "Long term"])
        self.service._find_live_torrent_for_job = MagicMock(return_value=None)

        self.service.retry_job(self.db, job_id=job_id)

        self.assertEqual(
            self.service.qbt.add_torrent.call_args.kwargs["tags"],
            ["torrent_intake", unique_tag, "Review", "Long term"],
        )

    def test_submit_without_custom_tags_skips_historical_reservation_query(self) -> None:
        self.service._reserved_custom_tags = MagicMock(
            wraps=self.service._reserved_custom_tags
        )

        self.submit([])

        self.service._reserved_custom_tags.assert_not_called()

    def test_failed_qbt_add_keeps_tags_in_durable_error_job(self) -> None:
        self.service.qbt.add_torrent.side_effect = RuntimeError("qB unavailable")

        with self.assertRaisesRegex(RuntimeError, "Failed to submit to qBittorrent"):
            self.submit(["Review"])

        job = self.db.query(Job).one()
        self.assertEqual(job.state, "error")
        self.assertEqual(job.custom_tags, ["Review"])

    def test_historical_private_tags_are_filtered_and_rejected(self) -> None:
        historical = Job(
            id="historic-job",
            magnet_uri=VALID_MAGNET,
            final_parent="/downloads/Movies",
            final_category=None,
            staging_preference="local",
            staging_actual="local",
            staging_root_initial="/staging-local",
            staging_root_actual="/staging-local",
            managed_tag="old_intake_tag",
            unique_tag="historic-private-tag",
            state="done",
        )
        self.db.add(historical)
        self.db.commit()
        self.service.qbt.list_tags.return_value = [
            "Review",
            "torrent_intake",
            "OLD_INTAKE_TAG",
            "HISTORIC-PRIVATE-TAG",
            "ti_job_orphaned",
        ]

        self.assertEqual(self.service.list_selectable_qbt_tags(self.db), ["Review"])
        with self.assertRaisesRegex(ValueError, "reserved for Torrent Intake"):
            self.submit(["old_intake_tag"])

    def test_hundreds_of_qbt_tags_remain_searchable_without_private_tags(self) -> None:
        reusable = [f"operator-tag-{index:03d}" for index in range(600)]
        self.service._reserved_custom_tags = MagicMock(
            wraps=self.service._reserved_custom_tags
        )
        self.service.qbt.list_tags.return_value = [
            *reversed(reusable),
            "torrent_intake",
            "ti_job_0123456789ab",
        ]

        selectable = self.service.list_selectable_qbt_tags(self.db)

        self.assertEqual(selectable, reusable)
        self.assertNotIn("torrent_intake", selectable)
        self.assertFalse(any(tag.casefold().startswith("ti_job_") for tag in selectable))
        historical_candidates = self.service._reserved_custom_tags.call_args.kwargs[
            "candidate_tags"
        ]
        self.assertEqual(historical_candidates, reusable)


if __name__ == "__main__":
    unittest.main()
