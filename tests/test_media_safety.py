from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from app.media_tools import MediaToolError, run_media_tool
from app.scanner import (
    MediaAttachment, MediaProbe, ScanInterrupted, ScannerIdentity, ScannerLimitError,
    ScannerPolicyError, ScannerService, file_identity, parse_large_media_probe, parse_scan_response,
)


class MediaSafetyTests(unittest.TestCase):
    def setUp(self):
        self.scanner = ScannerService()
        self.scanner.settings = self.scanner.settings.model_copy()
        self.identity = ScannerIdentity("clamd", "test", "1", None, self.scanner.policy_version(), "test")

    def test_inspection_warnings_never_become_infections(self):
        for name in ("Heuristics.Encrypted.Zip", "Heuristics.Encrypted.PDF",
                     "Heuristics.Broken.Media.JPEG", "Heuristics.Broken.Executable", "Broken.Media"):
            with self.subTest(name=name), self.assertRaises(ScannerPolicyError):
                parse_scan_response(f"stream: {name} FOUND")
        self.assertEqual(parse_scan_response("stream: Heuristics.Exploit.Test FOUND"),
                         (True, "Heuristics.Exploit.Test"))

    def test_only_known_size_limits_allow_a_media_retry(self):
        for limit, fallback, subdivide in (
            ("MaxFileSize", True, True), ("MaxScanSize", True, False),
            ("MaxRecursion", False, False), ("MaxFiles", False, False),
            ("PCREMatchLimit", False, False), ("NewUnknownLimit", False, False),
        ):
            with self.subTest(limit=limit):
                with self.assertRaises(ScannerLimitError) as caught:
                    parse_scan_response(f"stream: Heuristics.Limits.Exceeded.{limit} FOUND")
                self.assertEqual(caught.exception.can_use_media_fallback, fallback)
                self.assertEqual(caught.exception.can_subdivide, subdivide)

    def test_native_recursion_limit_does_not_enter_media_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "movie.mkv"
            path.write_bytes(b"test")
            with (
                patch.object(self.scanner, "_scan_descriptor_window", return_value=b"stream: Heuristics.Limits.Exceeded.MaxRecursion FOUND"),
                patch.object(self.scanner, "_scan_large_media_descriptor") as fallback,
                self.assertRaises(ScannerPolicyError),
            ):
                self.scanner.scan_path(str(path), identity=self.identity)
            fallback.assert_not_called()

    def test_expansion_limit_in_window_does_not_subdivide(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "movie.mkv"
            path.write_bytes(b"test")
            descriptor = os.open(path, os.O_RDONLY)
            try:
                with (
                    patch.object(self.scanner, "_probe_large_media_descriptor", return_value=MediaProbe("matroska")),
                    patch.object(self.scanner, "_scan_descriptor_window", return_value=b"stream: Heuristics.Limits.Exceeded.MaxScanSize FOUND") as scan,
                    patch("app.scanner.split_large_media_window") as split,
                    self.assertRaisesRegex(ScannerPolicyError, "cannot be resolved by splitting"),
                ):
                    self.scanner._scan_large_media_descriptor(descriptor, str(path), file_identity(os.fstat(descriptor)), heartbeat=None, should_stop=None)
                scan.assert_called_once()
                split.assert_not_called()
            finally:
                os.close(descriptor)

    def test_policy_revision_invalidates_checkpoints_even_with_legacy_environment_label(self):
        self.scanner.settings.scanner_policy_version = "my-unchanged-policy"
        current = self.scanner.policy_version()
        with patch("app.scanner.SCANNER_IMPLEMENTATION_POLICY", "older-implementation"):
            self.assertNotEqual(current, self.scanner.policy_version())
        self.scanner.settings.media_attachment_max_mib = 8
        self.assertNotEqual(current, self.scanner.policy_version())

    def test_attachment_budgets_and_indices_are_validated_before_extraction(self):
        attachment = {"index": 1, "codec_type": "attachment", "extradata_size": 12,
                      "tags": {"filename": "font.ttf"}}
        payload = {"format": {"format_name": "matroska"},
                   "streams": [{"index": 0, "codec_type": "video"}, attachment]}
        for size in (None, 0, -1, "12", True, 17):
            with self.subTest(size=size), self.assertRaises(ScannerPolicyError):
                attachment["extradata_size"] = size
                parse_large_media_probe(json.dumps(payload), "test", attachment_max_bytes=16)
        attachment["extradata_size"] = 12
        payload["streams"].append({**attachment, "index": 2})
        with self.assertRaisesRegex(ScannerPolicyError, "total extraction budget"):
            parse_large_media_probe(json.dumps(payload), "test", attachment_total_bytes=20)
        payload["streams"][2]["index"] = 1
        with self.assertRaisesRegex(ScannerPolicyError, "duplicate stream index"):
            parse_large_media_probe(json.dumps(payload), "test")

    def test_attached_picture_does_not_count_as_real_video(self):
        payload = {"format": {"format_name": "matroska"}, "streams": [
            {"index": 0, "codec_type": "video", "disposition": {"attached_pic": 1}, "tags": {"filename": "cover.png"}},
        ]}
        with self.assertRaisesRegex(ScannerPolicyError, "video stream"):
            parse_large_media_probe(json.dumps(payload), "test")

    def test_native_matroska_attachment_limit_is_held_without_chunking(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "movie.mkv"
            path.write_bytes(b"\x1aE\xdf\xa3test")
            probe = MediaProbe("matroska", (MediaAttachment(1, "../../escape.ttf", 4),))
            with (
                patch.object(self.scanner, "_probe_large_media_descriptor", return_value=probe),
                patch.object(self.scanner, "_run_media_tool", return_value=subprocess.CompletedProcess([], 0, b"data", b"")) as extract,
                patch.object(self.scanner, "_scan_descriptor_window", side_effect=[b"stream: OK", b"stream: Heuristics.Limits.Exceeded.MaxScanSize FOUND"]) as scan,
                patch.object(self.scanner, "_scan_large_media_descriptor") as fallback,
                self.assertRaises(ScannerPolicyError),
            ):
                self.scanner.scan_path(str(path), identity=self.identity)
            self.assertEqual(scan.call_count, 2)
            fallback.assert_not_called()
            command = extract.call_args.args[0]
            self.assertIn("pipe:1", command)
            self.assertNotIn("../../escape.ttf", command)

    def test_partial_attachment_output_blocks_the_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "movie.mkv"
            path.write_bytes(b"test")
            descriptor = os.open(path, os.O_RDONLY)
            try:
                with (
                    patch.object(self.scanner, "_run_media_tool", return_value=subprocess.CompletedProcess([], 0, b"short", b"")),
                    patch.object(self.scanner, "_scan_descriptor") as scan,
                    self.assertRaisesRegex(ScannerPolicyError, "incomplete"),
                ):
                    self.scanner._scan_media_attachments(
                        descriptor, str(path), file_identity(os.fstat(descriptor)),
                        MediaProbe("matroska", (MediaAttachment(1, "font.ttf", 10),)),
                        deadline=time.monotonic() + 30, heartbeat=None, should_stop=None,
                    )
                scan.assert_not_called()
            finally:
                os.close(descriptor)

    def test_invalid_probe_encoding_is_a_policy_failure(self):
        with (
            patch.object(self.scanner, "_run_media_tool", return_value=subprocess.CompletedProcess([], 0, b"\xff", b"")),
            self.assertRaisesRegex(ScannerPolicyError, "invalid media description"),
        ):
            self.scanner._probe_large_media_descriptor(0, "movie.mkv")

    def test_source_replacement_during_extraction_blocks_the_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "movie.mkv"
            path.write_bytes(b"test")
            descriptor = os.open(path, os.O_RDONLY)

            def replace_source(*args, **kwargs):
                replacement = Path(directory) / "replacement"
                replacement.write_bytes(b"test")
                replacement.replace(path)
                return subprocess.CompletedProcess([], 0, b"data", b"")

            try:
                with (
                    patch.object(self.scanner, "_run_media_tool", side_effect=replace_source),
                    patch.object(self.scanner, "_scan_descriptor") as scan,
                    self.assertRaisesRegex(RuntimeError, "changed"),
                ):
                    self.scanner._scan_media_attachments(
                        descriptor, str(path), file_identity(os.fstat(descriptor)),
                        MediaProbe("matroska", (MediaAttachment(1, "font.ttf", 4),)),
                        deadline=time.monotonic() + 30, heartbeat=None, should_stop=None,
                    )
                scan.assert_not_called()
            finally:
                os.close(descriptor)


class BoundedMediaProcessTests(unittest.TestCase):
    def run_tool(self, script, *, timeout=10, check_active=lambda: None, max_file_bytes=0, **kwargs):
        with tempfile.TemporaryFile() as handle:
            return run_media_tool([sys.executable, "-c", script], descriptor=handle.fileno(),
                                  deadline=time.monotonic() + timeout, max_file_bytes=max_file_bytes,
                                  check_active=check_active, **kwargs)

    def test_both_output_streams_are_bounded(self):
        for stream, count in (("stdout", 1025), ("stderr", 65537)):
            with self.subTest(stream=stream), self.assertRaisesRegex(MediaToolError, f"{stream} output limit"):
                self.run_tool(f"import sys; sys.{stream}.write('x'*{count})", max_stdout_bytes=1024)

    def test_timeout_reaps_a_child_with_closed_output_pipes(self):
        started = time.monotonic()
        with self.assertRaisesRegex(MediaToolError, "timed out"):
            self.run_tool("import os,time; os.close(1); os.close(2); time.sleep(30)", timeout=1)
        self.assertLess(time.monotonic() - started, 5)

    def test_cancellation_kills_and_reaps_the_process(self):
        original = subprocess.Popen
        processes = []
        calls = 0

        def spawn(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process

        def cancelled():
            nonlocal calls
            calls += 1
            if calls > 1:
                raise ScanInterrupted("paused")

        with patch("app.media_tools.subprocess.Popen", side_effect=spawn), self.assertRaises(ScanInterrupted):
            self.run_tool("import time; time.sleep(30)", check_active=cancelled)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].returncode)

    def test_subprocess_resource_limits_are_effective(self):
        result = self.run_tool("import json,resource; print(json.dumps([resource.getrlimit(k) for k in (resource.RLIMIT_AS,resource.RLIMIT_FSIZE,resource.RLIMIT_CORE)]))")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [[512 * 1024**2] * 2, [0, 0], [0, 0]])

    def test_helper_cannot_write_regular_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "output"
            result = self.run_tool(f"open({str(path)!r},'wb',buffering=0).write(b'payload')")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(path.stat().st_size, 0)
