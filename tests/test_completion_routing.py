from datetime import datetime
import logging
from types import SimpleNamespace
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Job
from app.service import JobService


class CompletionRoutingTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.service = JobService.__new__(JobService)
        self.service.settings = SimpleNamespace(completion_grace_seconds=15)
        self.service.logger = logging.getLogger(__name__)

    def job(self, job_id, *, torrent_hash="a" * 40, terminal=False):
        job = Job(
            id=job_id, magnet_uri="magnet:?xt=urn:btih:" + torrent_hash,
            final_parent="/downloads/Movies", staging_preference="local",
            staging_root_initial="/staging-local", managed_tag="torrent_intake",
            unique_tag="ti_job_" + job_id, qbt_hash=torrent_hash,
            state="done" if terminal else "downloading", is_terminal=terminal,
            content_path="/downloads/Movies/old" if terminal else None,
            promoted_at=datetime.utcnow() if terminal else None,
        )
        self.db.add(job)
        self.db.commit()
        return job

    def callback(self, *, torrent_hash="a" * 40, unique_tag=None, tags=None):
        return self.service.ingest_completion_event(
            self.db, qbt_hash=torrent_hash, qbt_hash_v2=None,
            unique_tag=unique_tag, tags=tags, torrent_name="New download",
            content_path="/staging-local/new", root_path=None, save_path=None, size_bytes=42,
        )

    def test_readded_torrent_callback_uses_new_tag_not_old_terminal_hash_match(self):
        old = self.job("old", terminal=True)
        new = self.job("new")
        result = self.callback(tags="torrent_intake, ti_job_new")
        self.assertEqual(result.id, new.id)
        self.assertEqual(new.state, "completion_event_received")
        self.assertEqual(new.content_path, "/staging-local/new")
        self.assertEqual(old.state, "done")
        self.assertEqual(old.content_path, "/downloads/Movies/old")

    def test_tagless_callback_prefers_sole_active_job_case_insensitively(self):
        old = self.job("old", terminal=True)
        new = self.job("new")
        result = self.callback(torrent_hash="A" * 40)
        self.assertEqual(result.id, new.id)
        self.assertEqual(new.state, "completion_event_received")
        self.assertIsNone(old.completion_event_received_at)

    def test_delayed_callback_for_retained_old_tag_does_not_update_readded_job(self):
        old = self.job("old", terminal=True)
        new = self.job("new")
        self.assertEqual(self.callback(unique_tag=old.unique_tag).id, old.id)
        self.assertEqual(new.state, "downloading")
        self.assertIsNone(new.completion_event_received_at)

    def test_deleted_or_unknown_old_tag_never_falls_back_to_new_job_hash(self):
        new = self.job("new")
        self.assertIsNone(self.callback(unique_tag="ti_job_deleted"))
        self.assertEqual(new.state, "downloading")
        self.assertIsNone(new.completion_event_received_at)

    def test_known_but_wrong_tag_cannot_rebind_to_another_tracked_hash(self):
        expected = self.job("expected")
        wrong = self.job("wrong", torrent_hash="b" * 40)
        self.assertIsNone(self.callback(unique_tag=wrong.unique_tag))
        self.assertEqual(wrong.qbt_hash, "b" * 40)
        self.assertEqual(wrong.state, "downloading")
        self.assertEqual(expected.state, "downloading")

    def test_conflicting_explicit_and_listed_tags_fail_closed(self):
        old = self.job("old", terminal=True)
        new = self.job("new")
        self.assertIsNone(self.callback(unique_tag=new.unique_tag, tags=old.unique_tag))
        self.assertIsNone(self.callback(tags=f"{old.unique_tag}, {new.unique_tag}"))
        self.assertEqual(new.state, "downloading")

    def test_tagless_callback_with_multiple_active_owners_fails_closed(self):
        first = self.job("first")
        second = self.job("second")
        self.assertIsNone(self.callback())
        self.assertEqual(first.state, "downloading")
        self.assertEqual(second.state, "downloading")

    def test_unique_tag_still_recovers_previously_untracked_hash(self):
        job = self.job("new", torrent_hash="b" * 40)
        self.assertEqual(self.callback(unique_tag=job.unique_tag).id, job.id)
        self.assertEqual(job.qbt_hash, "a" * 40)
        self.assertEqual(job.state, "completion_event_received")

    def test_terminal_history_does_not_block_new_tag_resolving_its_hash(self):
        old = self.job("old", terminal=True)
        new = self.job("new", torrent_hash="b" * 40)
        for previous_hash in (None, "b" * 40):
            with self.subTest(previous_hash=previous_hash):
                new.qbt_hash = previous_hash
                new.state = "waiting_for_qbt_hash"
                self.db.commit()
                self.assertEqual(self.callback(unique_tag=new.unique_tag).id, new.id)
                self.assertEqual(new.qbt_hash, "a" * 40)
                self.assertEqual(new.state, "completion_event_received")
                self.assertIsNone(old.completion_event_received_at)

    def test_terminal_only_hash_callback_remains_harmless(self):
        job = self.job("old", terminal=True)
        self.assertEqual(self.callback().id, job.id)
        self.assertEqual(job.state, "done")
        self.assertEqual(job.content_path, "/downloads/Movies/old")
        self.assertIsNone(job.completion_event_received_at)


if __name__ == "__main__":
    unittest.main()
