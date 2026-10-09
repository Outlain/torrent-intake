"""Archive-member checks using the real helper and the integration ClamD."""
from pathlib import Path
import hashlib
import os
import zipfile

from app.scanner import ScannerPolicyError, ScannerService
from archive_fixtures import rar4, rar5, zip_archive


def run_archive_checks(scanner: ScannerService, identity, root: Path, eicar: bytes) -> None:
    original_settings = scanner.settings
    external_storage = Path(os.environ.get("TI_TEST_ARCHIVE_SCRATCH", str(root / "external-archive-scratch")))
    external_storage.mkdir(exist_ok=True)
    marker = external_storage / ".mounted"
    marker.touch()
    scanner.settings = scanner.settings.model_copy(update={
        "scanner_max_file_mib": 1, "archive_free_space_buffer_gib": 0,
        "archive_max_expanded_gib": 1,
        "archive_scratch_dir": str(external_storage), "archive_scratch_mount_marker": str(marker),
    })
    torrent = root / "archive-torrent" / "Albums"
    torrent.mkdir(parents=True)
    archive = torrent / "Photos.rar"
    scratch = Path(scanner.settings.archive_workspace_root)
    first = b"first photo fixture\n" * 33000
    second = b"second photo fixture\n" * 33000
    try:
        for make in (rar4, rar5, lambda members: zip_archive(members, compression=zipfile.ZIP_STORED)):
            for infected in (False, True):
                payload = make([("Holiday/one.jpg", first),
                                ("Holiday/Nested/two.jpg", second + (eicar if infected else b""))])
                archive.write_bytes(payload)
                before = archive.stat()
                result = scanner.scan_path(str(archive), identity=identity)
                assert result.scan_method == "archive_members", result
                assert result.infected == infected and result.clean != infected, result
                if infected:
                    assert "EICAR" in result.threat_name, result
                    assert "Holiday/Nested/two.jpg" in result.raw_output, result
                after = archive.stat()
                assert (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) == (
                    after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                assert hashlib.sha256(archive.read_bytes()).digest() == hashlib.sha256(payload).digest()
                assert list(torrent.iterdir()) == [archive]
                assert not list(scratch.glob("scan-*")), "archive scratch copies leaked"
                assert not (Path(scanner.settings.data_dir) / "archive-scan").exists(), "custom storage fell back to data disk"
                print(f"PASS archive_members real ClamD infected={infected} format={make.__name__}; original torrent unchanged", flush=True)

        # Nested RARs share the parent's budget and still expose their payloads.
        archive.write_bytes(rar5([("padding-one.jpg", first), ("padding-two.jpg", second),
                                 ("nested.rar", rar4([("deep/test.txt", eicar)]))]))
        result = scanner.scan_path(str(archive), identity=identity)
        assert result.infected and "nested.rar!/deep/test.txt" in result.raw_output, result
        print("PASS nested archive member detection", flush=True)

        # This ZIP exceeds the normal daemon's 16 MiB expansion limit, while
        # every member fits the native stream boundary. Benchmark mode raises
        # the daemon's expansion budget to 4000 MiB, so a native clean verdict
        # is expected there. CI runs both modes to exercise both routes.
        archive.write_bytes(zip_archive([(f"photo-{index}.txt", first) for index in range(30)]))
        result = scanner.scan_path(str(archive), identity=identity)
        assert result.clean, result
        if os.environ.get("TI_TEST_BENCHMARK_WINDOWS") == "1":
            assert result.scan_method == "clamd_native", result
            assert "native-limit fallback" not in result.raw_output, result
            print("PASS native archive scan within benchmark daemon's expanded-data budget", flush=True)
        else:
            assert result.scan_method == "archive_members", result
            assert "native-limit fallback" in result.raw_output, result
            print("PASS real native expansion-limit fallback to archive member scans", flush=True)

        corrupt = rar5([("photo.jpg", first + second)])
        archive.write_bytes(corrupt[:-7])
        try:
            scanner.scan_path(str(archive), identity=identity)
        except ScannerPolicyError:
            print("PASS incomplete RAR remains held", flush=True)
        else:
            raise AssertionError("incomplete RAR was promoted")
        assert not list(scratch.glob("scan-*")), "archive scratch copies leaked"
    finally:
        scanner.settings = original_settings
