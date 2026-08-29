from __future__ import annotations

import unittest

from pydantic import ValidationError

from app.config import Settings
from app.schemas import JobBatchCreate, JobCreate
from app.tags import (
    MAX_CUSTOM_TAG_LENGTH,
    MAX_CUSTOM_TAGS,
    decode_custom_tags,
    encode_custom_tags,
    filter_selectable_custom_tags,
    normalize_custom_tags,
)


VALID_MAGNET = f"magnet:?xt=urn:btih:{'a' * 40}"


class CustomTagValidationTests(unittest.TestCase):
    def test_required_managed_tag_is_validated_at_startup(self) -> None:
        self.assertEqual(Settings(managed_tag="  torrent_intake  ").managed_tag, "torrent_intake")
        for managed_tag in (
            "",
            "review,later",
            "review\nnext",
            "\ud800",
            "ti_job_operator",
        ):
            with self.subTest(managed_tag=managed_tag), self.assertRaises(ValidationError):
                Settings(managed_tag=managed_tag)

    def test_normalizes_exact_duplicates_and_preserves_qbt_case_semantics(self) -> None:
        self.assertEqual(
            normalize_custom_tags([" Review ", "Review", "review", "Needs Work", "日本語"]),
            ["Review", "review", "Needs Work", "日本語"],
        )

    def test_rejects_qbt_invalid_and_intake_reserved_names(self) -> None:
        invalid_cases = (
            [""],
            ["   "],
            ["review,later"],
            ["review\nnext"],
            ["\ud800"],
            ["x" * (MAX_CUSTOM_TAG_LENGTH + 1)],
            ["torrent_intake"],
            ["TI_JOB_operator"],
        )
        for tags in invalid_cases:
            with self.subTest(tags=tags), self.assertRaises(ValueError):
                normalize_custom_tags(tags, reserved_tags=("torrent_intake",))

    def test_rejects_too_many_tags(self) -> None:
        with self.assertRaisesRegex(ValueError, f"at most {MAX_CUSTOM_TAGS}"):
            normalize_custom_tags([f"tag-{index}" for index in range(MAX_CUSTOM_TAGS + 1)])

    def test_filters_internal_invalid_and_duplicate_suggestions(self) -> None:
        self.assertEqual(
            filter_selectable_custom_tags(
                [
                    " review ",
                    "review",
                    "torrent_intake",
                    "OLD_MANAGED",
                    "ti_job_deadbeef",
                    "Ti_JoB_other",
                    "historic-unique",
                    "ti_jobbing",
                    "bad,tag",
                    "",
                    "日本語",
                ],
                reserved_tags=("torrent_intake", "old_managed", "historic-unique"),
            ),
            ["review", "ti_jobbing", "日本語"],
        )

    def test_json_round_trip_is_unicode_safe(self) -> None:
        encoded = encode_custom_tags(["Review", "日本語"])
        self.assertEqual(decode_custom_tags(encoded), ["Review", "日本語"])
        self.assertNotIn("\\u", encoded)

    def test_job_create_defaults_to_no_custom_tags(self) -> None:
        payload = JobCreate(magnet_uri=VALID_MAGNET, final_parent="/downloads/Movies")
        self.assertEqual(payload.custom_tags, [])

    def test_job_create_validates_and_normalizes_custom_tags(self) -> None:
        payload = JobCreate(
            magnet_uri=VALID_MAGNET,
            final_parent="/downloads/Movies",
            custom_tags=[" Review ", "Review", "review"],
        )
        self.assertEqual(payload.custom_tags, ["Review", "review"])

        with self.assertRaises(ValidationError):
            JobCreate(
                magnet_uri=VALID_MAGNET,
                final_parent="/downloads/Movies",
                custom_tags=["ti_job_not_allowed"],
            )

    def test_bulk_payload_keeps_per_job_tag_lists(self) -> None:
        payload = JobBatchCreate(
            jobs=[
                {
                    "magnet_uri": VALID_MAGNET,
                    "final_parent": "/downloads/Movies",
                    "custom_tags": ["Review", "Long term"],
                },
                {
                    "magnet_uri": f"magnet:?xt=urn:btih:{'c' * 40}",
                    "final_parent": "/downloads/Shows",
                    "custom_tags": ["Review"],
                },
            ]
        )
        self.assertEqual(payload.jobs[0].custom_tags, ["Review", "Long term"])
        self.assertEqual(payload.jobs[1].custom_tags, ["Review"])


if __name__ == "__main__":
    unittest.main()
