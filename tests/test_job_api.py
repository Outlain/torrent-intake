from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.main import create_job, create_jobs_bulk, qbt_tags
from app.schemas import JobBatchCreate, JobCreate


class JobApiTagForwardingTests(unittest.TestCase):
    def test_single_create_forwards_custom_tags(self) -> None:
        payload = JobCreate(
            magnet_uri=f"magnet:?xt=urn:btih:{'d' * 40}",
            final_parent="/downloads/Movies",
            custom_tags=["Review"],
        )
        db = MagicMock()

        with patch("app.main.service") as service:
            service.submit_job.return_value = "created-job"
            self.assertEqual(create_job(payload, db), "created-job")

        self.assertEqual(service.submit_job.call_args.kwargs["custom_tags"], ["Review"])

    def test_bulk_create_forwards_each_custom_tag_list(self) -> None:
        payload = JobBatchCreate(
            jobs=[
                {
                    "magnet_uri": f"magnet:?xt=urn:btih:{'e' * 40}",
                    "final_parent": "/downloads/Movies",
                    "custom_tags": ["Review", "Long term"],
                },
                {
                    "magnet_uri": f"magnet:?xt=urn:btih:{'f' * 40}",
                    "final_parent": "/downloads/Shows",
                    "custom_tags": ["Needs subtitles"],
                },
            ]
        )

        with patch("app.main.service") as service:
            service.submit_job.side_effect = ["job-one", "job-two"]
            result = create_jobs_bulk(payload, MagicMock())

        self.assertEqual(result["created"], 2)
        self.assertEqual(
            [call.kwargs["custom_tags"] for call in service.submit_job.call_args_list],
            [["Review", "Long term"], ["Needs subtitles"]],
        )

    def test_qbt_tag_endpoint_returns_only_service_filtered_tags(self) -> None:
        db = MagicMock()
        with patch("app.main.service") as service:
            service.list_selectable_qbt_tags.return_value = ["Review", "Needs subtitles"]
            result = qbt_tags(db)

        self.assertEqual(result, {"tags": ["Review", "Needs subtitles"]})
        service.list_selectable_qbt_tags.assert_called_once_with(db)


if __name__ == "__main__":
    unittest.main()
