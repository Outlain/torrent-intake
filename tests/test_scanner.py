from __future__ import annotations

import unittest
import os
import socket
import struct
import tempfile
import threading
from datetime import datetime, timedelta
from unittest.mock import patch
from pathlib import Path

from app.scanner import (
    ScannerUnavailable,
    ScannerIdentity,
    ScannerPolicyError,
    ScannerService,
    file_identity,
    parse_scan_response,
    parse_scanner_version,
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
        with self.assertRaises(ScannerPolicyError):
            parse_scan_response(
                "/downloads/large.iso: Heuristics.Limits.Exceeded.MaxFileSize FOUND"
            )

    def test_stream_limit_error_is_policy_error(self) -> None:
        with self.assertRaises(ScannerPolicyError):
            parse_scan_response("stream: INSTREAM size limit exceeded. ERROR")

    def test_normal_detection_is_infection(self) -> None:
        infected, threat = parse_scan_response(
            "/downloads/eicar.com: Win.Test.EICAR_HDB-1 FOUND"
        )

        self.assertTrue(infected)
        self.assertEqual(threat, "Win.Test.EICAR_HDB-1")


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

    def test_oversized_file_is_rejected_before_scanner_invocation(self) -> None:
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


class InStreamTests(unittest.TestCase):
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
