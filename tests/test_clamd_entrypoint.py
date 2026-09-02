import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


CLAMAV_DIR = Path(__file__).resolve().parents[1] / "clamav"
sys.path.insert(0, str(CLAMAV_DIR))
SPEC = importlib.util.spec_from_file_location(
    "torrent_intake_clamd_entrypoint", CLAMAV_DIR / "clamd_entrypoint.py"
)
assert SPEC is not None and SPEC.loader is not None
clamd_entrypoint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(clamd_entrypoint)


class ClamdEntrypointTests(unittest.TestCase):
    def test_max_scan_size_defaults_to_2000_mib(self):
        self.assertEqual(
            clamd_entrypoint.configured_max_scan_size_mib({}),
            2000,
        )

    def test_max_scan_size_accepts_4000_mib(self):
        self.assertEqual(
            clamd_entrypoint.configured_max_scan_size_mib(
                {"CLAMD_MAX_SCAN_SIZE_MIB": "4000"}
            ),
            4000,
        )

    def test_max_scan_size_rejects_invalid_values(self):
        for value in ("", "not-a-number", "0", "4001", "1.5"):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                clamd_entrypoint.configured_max_scan_size_mib(
                    {"CLAMD_MAX_SCAN_SIZE_MIB": value}
                )

    def test_render_changes_only_active_max_scan_size(self):
        base = "#MaxScanSize 1000M\nMaxScanSize 2000M\nMaxFileSize 2000M\n"
        rendered = clamd_entrypoint.render_runtime_config(base, 4000)
        self.assertEqual(
            rendered,
            "#MaxScanSize 1000M\nMaxScanSize 4000M\nMaxFileSize 2000M\n",
        )

    def test_render_requires_exactly_one_active_directive(self):
        for base in (
            "MaxFileSize 2000M\n",
            "MaxScanSize 1000M\nMaxScanSize 2000M\n",
        ):
            with self.subTest(base=base), self.assertRaises(RuntimeError):
                clamd_entrypoint.render_runtime_config(base, 4000)

    def test_runtime_config_is_private_and_base_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base_path = root / "clamd.conf"
            runtime_path = root / "runtime" / "clamd.conf"
            original = "MaxScanSize 2000M\nMaxFileSize 2000M\n"
            base_path.write_text(original, encoding="utf-8")

            written_path, value = clamd_entrypoint.write_runtime_config(
                base_path,
                runtime_path,
                {"CLAMD_MAX_SCAN_SIZE_MIB": "4000"},
            )

            self.assertEqual(written_path, runtime_path)
            self.assertEqual(value, 4000)
            self.assertEqual(base_path.read_text(encoding="utf-8"), original)
            self.assertEqual(
                runtime_path.read_text(encoding="utf-8"),
                "MaxScanSize 4000M\nMaxFileSize 2000M\n",
            )
            self.assertEqual(runtime_path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
