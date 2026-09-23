from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from app.scanner import (
    MediaAttachment, MediaProbe, ScanInterrupted, ScannerPolicyError,
    ScannerService, file_identity, parse_large_media_probe,
)


def chapter_probe(**changes):
    track = {"index": 3, "codec_type": "data", "codec_name": "bin_data",
             "codec_tag_string": "text", "nb_frames": "2",
             "tags": {"handler_name": "SubtitleHandler"}}
    track.update(changes)
    return {"format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"}, "streams": [
        {"index": 0, "codec_type": "video"}, track,
    ]}


class ChapterMediaTests(unittest.TestCase):
    def setUp(self):
        self.scanner = ScannerService()
        self.scanner.settings = self.scanner.settings.model_copy()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "episode.mp4"
        # Raw chapter sample, including a trailing box (not just a title).
        self.first = b"\0\x05Intro" + b"\0\0\0\x0cencd\0\0\x01\0"
        self.second = b"\0\x03End"
        self.raw = self.first + self.second
        self.path.write_bytes(b"header" + self.raw + b"other media data")
        self.descriptor = os.open(self.path, os.O_RDONLY)
        self.addCleanup(os.close, self.descriptor)
        self.expected = file_identity(os.fstat(self.descriptor))
        self.track = MediaAttachment(3, "chapter-track-3.text", None, chapter_samples=2)
        self.manifest = {
            "streams": [{"index": 3, "codec_type": "subtitle", "codec_name": "mov_text",
                         "codec_tag_string": "text", "nb_frames": "2"}],
            "packets": [
                {"stream_index": 3, "pos": "6", "size": str(len(self.first)), "flags": "K_"},
                {"stream_index": 3, "pos": str(6 + len(self.first)), "size": str(len(self.second)), "flags": "K_"},
            ],
        }

    def output(self, manifest=None, stderr=b""):
        return subprocess.CompletedProcess([], 0, json.dumps(
            self.manifest if manifest is None else manifest).encode(), stderr)

    def extract(self, **overrides):
        options = dict(maximum=1024, deadline=time.monotonic() + 30, heartbeat=None, should_stop=None)
        options.update(overrides)
        return self.scanner._read_mp4_chapter_track(
            self.descriptor, str(self.path), self.expected, self.track, **options,
        )

    def test_exact_candidate_is_admitted_independent_of_handler_label(self):
        for tags in ({}, {"handler_name": "SubtitleHandler"}, {"handler_name": "Custom chapter label"}):
            probe = parse_large_media_probe(json.dumps(chapter_probe(tags=tags)), str(self.path))
            self.assertEqual(probe.attachments, (self.track,))

    def test_other_data_types_or_containers_are_not_admitted(self):
        for changes in ({"codec_tag_string": "tmcd"}, {"codec_tag_string": "gpmd"},
                        {"codec_name": "unknown"}, {"codec_tag_string": None}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ScannerPolicyError, "unsupported stream"):
                parse_large_media_probe(json.dumps(chapter_probe(**changes)), str(self.path))
        payload = chapter_probe()
        payload["format"]["format_name"] = "matroska"
        with self.assertRaises(ScannerPolicyError):
            parse_large_media_probe(json.dumps(payload), str(self.path))

    def test_sample_counts_and_combined_attachment_budgets_are_bounded(self):
        for count in (None, "N/A", "", "0", "4097", -1, True, 1.5, "2.0"):
            with self.subTest(count=count), self.assertRaisesRegex(ScannerPolicyError, "sample count"):
                parse_large_media_probe(json.dumps(chapter_probe(nb_frames=count)), str(self.path))
        payload = chapter_probe()
        payload["streams"].append({"index": 4, "codec_type": "attachment", "extradata_size": 1,
                                   "tags": {"filename": "font.ttf"}})
        with self.assertRaisesRegex(ScannerPolicyError, "total extraction budget"):
            parse_large_media_probe(json.dumps(payload), str(self.path), attachment_max_bytes=16,
                                    attachment_total_bytes=16)
        payload["streams"][2] = dict(payload["streams"][1])
        with self.assertRaisesRegex(ScannerPolicyError, "duplicate stream index"):
            parse_large_media_probe(json.dumps(payload), str(self.path))

    def test_raw_samples_and_trailing_boxes_are_preserved_without_movie_copy(self):
        with patch.object(self.scanner, "_run_media_tool", return_value=self.output()) as tool:
            self.assertEqual(self.extract(), self.raw)
        command = tool.call_args.args[0]
        self.assertEqual(command[command.index("-ignore_chapters") + 1], "1")
        self.assertEqual(command[command.index("-select_streams") + 1], "3")
        self.assertIn("-show_packets", command)
        self.assertNotIn("-show_data", command)
        self.assertIn(f"/proc/self/fd/{self.descriptor}", command)
        self.assertNotIn(str(self.path), command)

    def test_empty_partial_or_reclassified_packet_manifests_are_rejected(self):
        cases = []
        for packets in ([], self.manifest["packets"][:1], self.manifest["packets"] * 2, None):
            cases.append({**self.manifest, "packets": packets})
        for key, value in (("codec_name", "bin_data"), ("codec_tag_string", "tx3g"),
                           ("nb_frames", "3"), ("index", True), ("codec_type", "data")):
            cases.append({**self.manifest, "streams": [{**self.manifest["streams"][0], key: value}]})
        cases.extend(({}, {"streams": None, "packets": []}))
        for payload in cases:
            with (self.subTest(payload=payload),
                  patch.object(self.scanner, "_run_media_tool", return_value=self.output(payload)),
                  self.assertRaises(ScannerPolicyError)):
                self.extract()

    def test_bad_packet_offsets_lengths_flags_and_track_indices_are_rejected(self):
        for key, value in (("pos", "-1"), ("pos", "9999"), ("pos", True),
                           ("size", "1"), ("size", "9999"), ("size", "1.0"),
                           ("stream_index", 0), ("flags", "C_"), ("flags", None)):
            payload = copy.deepcopy(self.manifest)
            payload["packets"][0][key] = value
            with (self.subTest(key=key, value=value),
                  patch.object(self.scanner, "_run_media_tool", return_value=self.output(payload)),
                  self.assertRaisesRegex(ScannerPolicyError, "packet validation")):
                self.extract()
        payload = copy.deepcopy(self.manifest)
        payload["packets"][1]["pos"] = "6"
        with (patch.object(self.scanner, "_run_media_tool", return_value=self.output(payload)),
              self.assertRaisesRegex(ScannerPolicyError, "overlapping")):
            self.extract()

    def test_short_read_invalid_text_prefix_and_budget_exhaustion_are_rejected(self):
        with patch.object(self.scanner, "_run_media_tool", return_value=self.output()):
            for value in (b"", b"\xff\xff" + self.first[2:]):
                with (self.subTest(value=value), patch("app.scanner.os.pread", return_value=value),
                      self.assertRaisesRegex(ScannerPolicyError, "truncated packet")):
                    self.extract()
            with self.assertRaisesRegex(ScannerPolicyError, "oversized packet"):
                self.extract(maximum=len(self.raw) - 1)

    def test_demux_errors_timeout_cancellation_and_replacement_stay_blocked(self):
        with (patch.object(self.scanner, "_run_media_tool", return_value=self.output(stderr=b"corrupt packet")),
              self.assertRaisesRegex(ScannerPolicyError, "demuxing error")):
            self.extract()
        with patch.object(self.scanner, "_run_media_tool", return_value=self.output()):
            with self.assertRaisesRegex(ScannerPolicyError, "timed out"):
                self.extract(deadline=time.monotonic() - 1)
            with self.assertRaises(ScanInterrupted):
                self.extract(should_stop=lambda: True)
            with self.assertRaises(ScanInterrupted):
                self.extract(heartbeat=lambda: False)
            replacement = self.path.with_name("replacement")
            replacement.write_bytes(self.path.read_bytes())
            replacement.replace(self.path)
            with self.assertRaisesRegex(RuntimeError, "changed"):
                self.extract()

    def test_complete_payload_is_scanned_and_never_subdivided_or_skipped(self):
        for reply in (b"stream: OK", b"stream: Test.EICAR FOUND",
                      b"stream: Heuristics.Limits.Exceeded.MaxScanSize FOUND"):
            scanned = []

            def scan(descriptor, **kwargs):
                scanned.append(os.pread(descriptor, kwargs["length"], kwargs["offset"]))
                return reply

            with (self.subTest(reply=reply),
                  patch.object(self.scanner, "_run_media_tool", return_value=self.output()),
                  patch.object(self.scanner, "_scan_descriptor_window", side_effect=scan),
                  patch.object(self.scanner, "_scan_large_media_descriptor") as fallback):
                def run():
                    return self.scanner._scan_media_attachments(
                        self.descriptor, str(self.path), self.expected, MediaProbe("mov", (self.track,)),
                        deadline=time.monotonic() + 30, heartbeat=None, should_stop=None,
                    )
                if b"Limits.Exceeded" in reply:
                    with self.assertRaises(ScannerPolicyError):
                        run()
                else:
                    infected, _, _ = run()
                    self.assertEqual(infected, b"FOUND" in reply)
                self.assertEqual(scanned, [self.raw])
                fallback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
