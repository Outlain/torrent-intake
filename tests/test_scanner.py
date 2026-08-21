from __future__ import annotations

import json
import os
import socket
import struct
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest.mock import patch
from pathlib import Path

from app.scanner import (
    ScannerUnavailable,
    ScannerIdentity,
    ScannerLimitError,
    ScannerPolicyError,
    ScannerService,
    file_identity,
    large_media_window_ranges,
    parse_large_media_probe,
    parse_scan_response,
    parse_scanner_version,
    split_large_media_window,
)


class ScannerParsingTests(unittest.TestCase):
    def test_parses_engine_database_and_date(self) -> None:
        engine, database, updated_at = parse_scanner_version(
            "ClamAV 1.5.3/27901/Wed Jul 29 08:00:00 2026"
        )

        self.assertEqual(engine, "1.5.3")
        self.assertEqual(database, "27901")
        self.assertEqual(updated_at, datetime(2026, 7, 29, 8, 0, 0))

    def test_limit_detection_is_policy_error_not_infection(self) -> None:
        with self.assertRaises(ScannerLimitError):
            parse_scan_response(
                "/downloads/large.iso: Heuristics.Limits.Exceeded.MaxFileSize FOUND"
            )

    def test_stream_limit_error_is_policy_error(self) -> None:
        with self.assertRaises(ScannerLimitError):
            parse_scan_response("stream: INSTREAM size limit exceeded. ERROR")

    def test_normal_detection_is_infection(self) -> None:
        infected, threat = parse_scan_response(
            "/downloads/eicar.com: Win.Test.EICAR_HDB-1 FOUND"
        )

        self.assertTrue(infected)
        self.assertEqual(threat, "Win.Test.EICAR_HDB-1")

    def test_large_media_probe_requires_real_video_and_safe_attachments(self) -> None:
        payload = {
            "format": {"format_name": "matroska,webm"},
            "streams": [
                {"codec_type": "video", "codec_name": "hevc"},
                {
                    "codec_type": "attachment",
                    "codec_name": "ttf",
                    "tags": {"filename": "subtitle-font.ttf"},
                },
            ],
        }
        self.assertEqual(
            parse_large_media_probe(json.dumps(payload), "/downloads/movie.mkv"),
            "matroska,webm",
        )

        payload["streams"][1]["tags"]["filename"] = "payload.exe"
        with self.assertRaisesRegex(ScannerPolicyError, "attachment"):
            parse_large_media_probe(json.dumps(payload), "/downloads/movie.mkv")

    def test_large_media_probe_rejects_archive_and_audio_only_content(self) -> None:
        with self.assertRaisesRegex(ScannerPolicyError, "not an approved video"):
            parse_large_media_probe(
                json.dumps(
                    {
                        "format": {"format_name": "zip"},
                        "streams": [{"codec_type": "video"}],
                    }
                ),
                "/downloads/archive.zip",
            )
        with self.assertRaisesRegex(ScannerPolicyError, "video stream"):
            parse_large_media_probe(
                json.dumps(
                    {
                        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
                        "streams": [{"codec_type": "audio"}],
                    }
                ),
                "/downloads/audio.m4a",
            )

    def test_large_media_windows_cover_every_byte_with_overlap(self) -> None:
        ranges = large_media_window_ranges(25, 10, 2)
        self.assertEqual(ranges, [(0, 10), (8, 10), (16, 9)])
        coverage = [False] * 25
        for offset, length in ranges:
            for index in range(offset, offset + length):
                coverage[index] = True
        self.assertTrue(all(coverage))

    def test_adaptive_split_preserves_coverage_and_overlap(self) -> None:
        split = split_large_media_window(100, 1000, 400, 20)

        self.assertIsNotNone(split)
        left, right = split or ((0, 0), (0, 0))
        self.assertEqual(left[0], 100)
        self.assertEqual(left[0] + left[1], right[0] + 20)
        self.assertEqual(right[0] + right[1], 1100)
        self.assertGreaterEqual(left[1], 400)
        self.assertGreaterEqual(right[1], 400)

    def test_adaptive_split_stops_before_children_would_be_too_small(self) -> None:
        self.assertIsNone(split_large_media_window(0, 700, 400, 20))


class ScannerHealthTests(unittest.TestCase):
    def test_stale_definitions_block_new_scans(self) -> None:
        service = ScannerService()
        service.settings.scanner_definitions_warn_hours = 24
        service.settings.scanner_definitions_stale_hours = 48
        old_date = (datetime.utcnow() - timedelta(hours=72)).strftime(
            "%a %b %d %H:%M:%S %Y"
        )

        with patch.object(
            service,
            "_version_output",
            return_value=f"ClamAV 1.5.3/27901/{old_date}",
        ):
            health = service.health(force=True)

        self.assertEqual(health.status, "stale")
        self.assertFalse(health.can_scan)
        self.assertGreaterEqual(health.definitions_age_hours or 0, 71)

    def test_oversized_non_media_file_is_rejected_without_native_scan(self) -> None:
        service = ScannerService()
        identity = ScannerIdentity(
            backend="command",
            engine_version="1.5.3",
            database_version="27901",
            database_updated_at=datetime.utcnow(),
            policy_version=service.policy_version(),
            raw_version="ClamAV 1.5.3/27901/test",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "too-large.bin"
            path.write_bytes(b"12345")
            original_limit = service.settings.scanner_max_file_mib
            service.settings.scanner_max_file_mib = 0
            try:
                with self.assertRaises(ScannerPolicyError):
                    service.scan_path(str(path), identity=identity)
            finally:
                service.settings.scanner_max_file_mib = original_limit

    def test_oversized_media_routes_to_large_media_backend(self) -> None:
        service = ScannerService()
        identity = ScannerIdentity(
            backend="clamd-instream",
            engine_version="1.5.3",
            database_version="27901",
            database_updated_at=datetime.utcnow(),
            policy_version=service.policy_version(),
            raw_version="ClamAV 1.5.3/27901/test",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "movie.mkv"
            path.write_bytes(b"large-media-placeholder")
            original_limit = service.settings.scanner_max_file_mib
            service.settings.scanner_max_file_mib = 0
            try:
                with (
                    patch.object(
                        service,
                        "_scan_large_media_descriptor",
                        return_value=(False, None, "all bytes scanned"),
                    ) as large_scan,
                    patch.object(service, "_scan_descriptor") as native_scan,
                ):
                    result = service.scan_path(str(path), identity=identity)
                self.assertTrue(result.clean)
                self.assertEqual(result.scan_method, "large_media_parallel_adaptive_windows")
                large_scan.assert_called_once()
                native_scan.assert_not_called()
            finally:
                service.settings.scanner_max_file_mib = original_limit

    def test_native_scan_limit_retries_verified_media_with_bounded_windows(self) -> None:
        service = ScannerService()
        identity = ScannerIdentity(
            backend="clamd-instream",
            engine_version="1.5.3",
            database_version="27901",
            database_updated_at=datetime.utcnow(),
            policy_version=service.policy_version(),
            raw_version="ClamAV 1.5.3/27901/test",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "movie.mkv"
            path.write_bytes(b"small-test-placeholder")
            with (
                patch.object(
                    service,
                    "_scan_descriptor_window",
                    side_effect=[
                        b"stream: Heuristics.Limits.Exceeded.MaxScanSize FOUND",
                        b"stream: OK",
                    ],
                ) as stream_scan,
                patch.object(service, "_probe_large_media_descriptor", return_value="matroska"),
            ):
                result = service.scan_path(str(path), identity=identity)

            self.assertEqual(stream_scan.call_count, 2)
            self.assertEqual([call.kwargs["offset"] for call in stream_scan.call_args_list], [0, 0])
            self.assertTrue(result.clean)
            self.assertEqual(result.scan_method, "large_media_parallel_adaptive_windows")
            self.assertIn("native-limit fallback", result.raw_output)

    def test_native_scan_limit_keeps_non_media_fallback_held(self) -> None:
        service = ScannerService()
        identity = ScannerIdentity(
            backend="clamd-instream",
            engine_version="1.5.3",
            database_version="27901",
            database_updated_at=datetime.utcnow(),
            policy_version=service.policy_version(),
            raw_version="ClamAV 1.5.3/27901/test",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "archive.zip"
            path.write_bytes(b"small-test-placeholder")
            with (
                patch.object(
                    service,
                    "_scan_descriptor",
                    side_effect=ScannerLimitError(
                        "ClamAV limit: Heuristics.Limits.Exceeded.MaxScanSize"
                    ),
                ),
                patch.object(
                    service,
                    "_scan_large_media_descriptor",
                    side_effect=ScannerPolicyError("not an approved video container"),
                ),
                self.assertRaisesRegex(
                    ScannerPolicyError,
                    "verified-media fallback was rejected.*not an approved video",
                ),
            ):
                service.scan_path(str(path), identity=identity)


class InStreamTests(unittest.TestCase):
    def test_large_media_scans_every_planned_window_and_records_method(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "movie.mkv"
            path.write_bytes(b"0123456789abcdefghijklmnop")
            descriptor = os.open(path, os.O_RDONLY)
            try:
                service = ScannerService()
                expected = file_identity(os.fstat(descriptor))
                planned = [(0, 10), (8, 10), (16, 10)]
                with (
                    patch("app.scanner.large_media_window_ranges", return_value=planned),
                    patch.object(service, "_probe_large_media_descriptor", return_value="matroska"),
                    patch.object(
                        service,
                        "_scan_descriptor_window",
                        side_effect=[b"stream: OK", b"stream: OK", b"stream: OK"],
                    ) as stream_window,
                ):
                    infected, threat, output = service._scan_large_media_descriptor(
                        descriptor,
                        str(path),
                        expected,
                        heartbeat=None,
                        should_stop=None,
                    )

                self.assertFalse(infected)
                self.assertIsNone(threat)
                self.assertIn("coverage=all-bytes", output)
                self.assertEqual(
                    [call.kwargs["offset"] for call in stream_window.call_args_list],
                    [0, 8, 16],
                )
                self.assertEqual(
                    [call.kwargs["length"] for call in stream_window.call_args_list],
                    [10, 10, 10],
                )
            finally:
                os.close(descriptor)

    def test_large_media_uses_four_parallel_window_workers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "movie.mkv"
            path.write_bytes(b"0123456789abcdefghijklmnop")
            descriptor = os.open(path, os.O_RDONLY)
            service = ScannerService()
            original_workers = service.settings.per_job_scan_workers
            service.settings.per_job_scan_workers = 4
            expected = file_identity(os.fstat(descriptor))
            active = 0
            maximum_active = 0
            lock = threading.Lock()
            four_started = threading.Event()

            def scan_window(*_args, **_kwargs) -> bytes:
                nonlocal active, maximum_active
                with lock:
                    active += 1
                    maximum_active = max(maximum_active, active)
                    if active == 4:
                        four_started.set()
                try:
                    self.assertTrue(four_started.wait(2))
                    time.sleep(0.02)
                    return b"stream: OK"
                finally:
                    with lock:
                        active -= 1

            try:
                with (
                    patch(
                        "app.scanner.large_media_window_ranges",
                        return_value=[(0, 10), (4, 10), (8, 10), (16, 10)],
                    ),
                    patch.object(service, "_probe_large_media_descriptor", return_value="matroska"),
                    patch.object(service, "_scan_descriptor_window", side_effect=scan_window),
                ):
                    infected, threat, output = service._scan_large_media_descriptor(
                        descriptor,
                        str(path),
                        expected,
                        heartbeat=None,
                        should_stop=None,
                    )

                self.assertFalse(infected)
                self.assertIsNone(threat)
                self.assertEqual(maximum_active, 4)
                self.assertIn("workers=4", output)
            finally:
                service.settings.per_job_scan_workers = original_workers
                os.close(descriptor)

    def test_large_media_limit_adaptively_subdivides_the_window(self) -> None:
        mebibyte = 1024 * 1024
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "movie.mkv"
            with path.open("wb") as stream:
                stream.truncate(4 * mebibyte)
            descriptor = os.open(path, os.O_RDONLY)
            service = ScannerService()
            original_workers = service.settings.per_job_scan_workers
            original_minimum = service.settings.large_media_min_chunk_mib
            original_overlap = service.settings.large_media_overlap_kib
            service.settings.per_job_scan_workers = 1
            service.settings.large_media_min_chunk_mib = 1
            service.settings.large_media_overlap_kib = 0
            expected = file_identity(os.fstat(descriptor))
            lengths: list[int] = []

            def scan_window(*_args, **kwargs) -> bytes:
                lengths.append(kwargs["length"])
                if kwargs["length"] > 2 * mebibyte:
                    return b"stream: Heuristics.Limits.Exceeded.MaxScanSize FOUND"
                return b"stream: OK"

            try:
                with (
                    patch(
                        "app.scanner.large_media_window_ranges",
                        return_value=[(0, 4 * mebibyte)],
                    ),
                    patch.object(service, "_probe_large_media_descriptor", return_value="matroska"),
                    patch.object(service, "_scan_descriptor_window", side_effect=scan_window),
                ):
                    infected, threat, output = service._scan_large_media_descriptor(
                        descriptor,
                        str(path),
                        expected,
                        heartbeat=None,
                        should_stop=None,
                    )

                self.assertFalse(infected)
                self.assertIsNone(threat)
                self.assertEqual(lengths, [4 * mebibyte, 2 * mebibyte, 2 * mebibyte])
                self.assertIn("clamd_requests=3", output)
                self.assertIn("subdividing", output)
            finally:
                service.settings.per_job_scan_workers = original_workers
                service.settings.large_media_min_chunk_mib = original_minimum
                service.settings.large_media_overlap_kib = original_overlap
                os.close(descriptor)

    def test_large_media_limit_at_minimum_window_fails_closed(self) -> None:
        mebibyte = 1024 * 1024
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "movie.mkv"
            with path.open("wb") as stream:
                stream.truncate(mebibyte)
            descriptor = os.open(path, os.O_RDONLY)
            service = ScannerService()
            original_workers = service.settings.per_job_scan_workers
            original_minimum = service.settings.large_media_min_chunk_mib
            original_overlap = service.settings.large_media_overlap_kib
            service.settings.per_job_scan_workers = 1
            service.settings.large_media_min_chunk_mib = 1
            service.settings.large_media_overlap_kib = 0
            expected = file_identity(os.fstat(descriptor))

            try:
                with (
                    patch(
                        "app.scanner.large_media_window_ranges",
                        return_value=[(0, mebibyte)],
                    ),
                    patch.object(service, "_probe_large_media_descriptor", return_value="matroska"),
                    patch.object(
                        service,
                        "_scan_descriptor_window",
                        return_value=b"stream: Heuristics.Limits.Exceeded.MaxScanSize FOUND",
                    ),
                    self.assertRaisesRegex(ScannerPolicyError, "adaptive minimum window"),
                ):
                    service._scan_large_media_descriptor(
                        descriptor,
                        str(path),
                        expected,
                        heartbeat=None,
                        should_stop=None,
                    )
            finally:
                service.settings.per_job_scan_workers = original_workers
                service.settings.large_media_min_chunk_mib = original_minimum
                service.settings.large_media_overlap_kib = original_overlap
                os.close(descriptor)

    def test_parallel_window_error_prevents_clean_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "movie.mkv"
            path.write_bytes(b"0123456789abcdefghijklmnop")
            descriptor = os.open(path, os.O_RDONLY)
            service = ScannerService()
            original_workers = service.settings.per_job_scan_workers
            service.settings.per_job_scan_workers = 2
            expected = file_identity(os.fstat(descriptor))

            def scan_window(*_args, **kwargs) -> bytes:
                if kwargs["offset"] == 0:
                    raise RuntimeError("simulated ClamD failure")
                return b"stream: OK"

            try:
                with (
                    patch(
                        "app.scanner.large_media_window_ranges",
                        return_value=[(0, 10), (8, 10)],
                    ),
                    patch.object(service, "_probe_large_media_descriptor", return_value="matroska"),
                    patch.object(service, "_scan_descriptor_window", side_effect=scan_window),
                    self.assertRaisesRegex(RuntimeError, "simulated ClamD failure"),
                ):
                    service._scan_large_media_descriptor(
                        descriptor,
                        str(path),
                        expected,
                        heartbeat=None,
                        should_stop=None,
                    )
            finally:
                service.settings.per_job_scan_workers = original_workers
                os.close(descriptor)

    def test_global_clamd_limit_allows_only_four_active_requests(self) -> None:
        service = ScannerService()
        service._clamd_scan_slots = threading.BoundedSemaphore(4)
        active = 0
        maximum_active = 0
        lock = threading.Lock()
        four_started = threading.Event()

        class FakeSocket:
            def __enter__(inner_self):
                nonlocal active, maximum_active
                with lock:
                    active += 1
                    maximum_active = max(maximum_active, active)
                    if active == 4:
                        four_started.set()
                if not four_started.wait(2):
                    raise RuntimeError("four requests did not start concurrently")
                time.sleep(0.02)
                return inner_self

            def __exit__(inner_self, *_args):
                nonlocal active
                with lock:
                    active -= 1

            def settimeout(inner_self, _timeout):
                return None

            def connect(inner_self, _path):
                return None

            def sendall(inner_self, _payload):
                return None

            def recv(inner_self, _length):
                return b"stream: OK\0"

        deadline = time.monotonic() + 10

        def request(_index: int) -> bytes:
            return service._scan_descriptor_window(
                -1,
                offset=0,
                length=0,
                deadline=deadline,
                timeout_description="test timeout",
                heartbeat=None,
                should_stop=None,
            )

        with (
            patch("app.scanner.socket.socket", side_effect=lambda *_args, **_kwargs: FakeSocket()),
            ThreadPoolExecutor(max_workers=8) as executor,
        ):
            replies = list(executor.map(request, range(8)))

        self.assertEqual(replies, [b"stream: OK"] * 8)
        self.assertEqual(maximum_active, 4)

    def test_total_scan_deadline_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "payload.bin"
            path.write_bytes(b"content")
            descriptor = os.open(path, os.O_RDONLY)
            try:
                service = ScannerService()
                service.settings.scanner_scan_timeout_seconds = 60
                expected = file_identity(os.fstat(descriptor))
                with (
                    patch("app.scanner.socket.socket") as socket_factory,
                    patch("app.scanner.time.monotonic", side_effect=[0, 1, 61]),
                    self.assertRaisesRegex(ScannerUnavailable, "60-second timeout"),
                ):
                    socket_factory.return_value.__enter__.return_value = (
                        socket_factory.return_value
                    )
                    service._scan_descriptor(
                        descriptor,
                        str(path),
                        expected,
                        heartbeat=None,
                        should_stop=None,
                    )
            finally:
                os.close(descriptor)

    def _serve_once(
        self,
        socket_path: Path,
        received: list[bytes],
        reply: bytes,
        before_reply: threading.Event | None = None,
        continue_reply: threading.Event | None = None,
    ) -> threading.Thread:
        ready = threading.Event()

        def server() -> None:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(socket_path))
                listener.listen(1)
                ready.set()
                connection, _ = listener.accept()
                with connection:
                    command = self._recv_exact(connection, len(b"zINSTREAM\0"))
                    self.assertEqual(command, b"zINSTREAM\0")
                    content = bytearray()
                    while True:
                        length_raw = self._recv_exact(connection, 4)
                        length = struct.unpack("!I", length_raw)[0]
                        if length == 0:
                            break
                        content.extend(self._recv_exact(connection, length))
                    received.append(bytes(content))
                    if before_reply:
                        before_reply.set()
                    if continue_reply:
                        continue_reply.wait(5)
                    connection.sendall(reply + b"\0")

        thread = threading.Thread(target=server, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(2))
        return thread

    @staticmethod
    def _recv_exact(connection: socket.socket, length: int) -> bytes:
        result = bytearray()
        while len(result) < length:
            chunk = connection.recv(length - len(result))
            if not chunk:
                raise RuntimeError("client disconnected")
            result.extend(chunk)
        return bytes(result)

    @staticmethod
    def _identity(service: ScannerService) -> ScannerIdentity:
        return ScannerIdentity(
            backend="clamd-instream",
            engine_version="1.4.5",
            database_version="1",
            database_updated_at=datetime.utcnow(),
            policy_version=service.policy_version(),
            raw_version="ClamAV 1.4.5/1/test",
        )

    def test_streams_descriptor_content_and_accepts_newline_filename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "line\nbreak.bin"
            path.write_bytes(b"safe content")
            socket_path = root / "clamd.sock"
            received: list[bytes] = []
            thread = self._serve_once(socket_path, received, b"stream: OK")
            service = ScannerService()
            service.settings.clamd_socket_path = str(socket_path)

            result = service.scan_path(str(path), identity=self._identity(service))
            thread.join(2)

            self.assertTrue(result.clean)
            self.assertEqual(received, [b"safe content"])

    def test_replaced_path_is_not_recorded_clean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "payload.bin"
            path.write_bytes(b"first")
            socket_path = root / "clamd.sock"
            received: list[bytes] = []
            before_reply = threading.Event()
            continue_reply = threading.Event()
            thread = self._serve_once(
                socket_path,
                received,
                b"stream: OK",
                before_reply,
                continue_reply,
            )
            service = ScannerService()
            service.settings.clamd_socket_path = str(socket_path)

            error: list[Exception] = []

            def scan() -> None:
                try:
                    service.scan_path(str(path), identity=self._identity(service))
                except Exception as exc:
                    error.append(exc)

            scan_thread = threading.Thread(target=scan)
            scan_thread.start()
            self.assertTrue(before_reply.wait(2))
            path.unlink()
            path.write_bytes(b"replacement")
            continue_reply.set()
            scan_thread.join(3)
            thread.join(2)
            self.assertEqual(len(error), 1)
            self.assertIn("identity changed", str(error[0]))


if __name__ == "__main__":
    unittest.main()
