from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from app.scanner import (
    ScannerIdentity,
    ScannerPolicyError,
    ScannerService,
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
        fake_stat = SimpleNamespace(st_size=service.settings.scanner_max_file_bytes + 1)

        with (
            patch.object(service, "require_healthy", return_value=identity),
            patch("app.scanner.os.stat", return_value=fake_stat),
            self.assertRaises(ScannerPolicyError),
        ):
            service.scan_path("/downloads/too-large.bin")


if __name__ == "__main__":
    unittest.main()
