"""Real ffprobe + Unix-socket ClamD tests; run via run_media_integration.sh."""
from __future__ import annotations

import os
import hashlib
import io
from pathlib import Path
import subprocess
import sys
import time
import zipfile
from unittest.mock import PropertyMock, patch

from app.scanner import ScannerIdentity, ScannerPolicyError, ScannerService, file_identity


ROOT = Path("/test")
# The harmless, standard antivirus test string, not malware.
EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
HASH_ONLY_ATTACHMENT = b"\0\xffSynthetic attachment hash test; not malware.\0" * 32


def prepare() -> None:
    definitions = ROOT / "defs"
    definitions.mkdir()
    (definitions / "test.ndb").write_text(f"Test.EICAR:0:*:{EICAR.hex()}\n")
    (definitions / "test.hdb").write_text(
        f"{hashlib.md5(HASH_ONLY_ATTACHMENT).hexdigest()}:{len(HASH_ONLY_ATTACHMENT)}:Test.AttachmentHash\n"
    )
    benchmark = os.environ.get("TI_TEST_BENCHMARK_WINDOWS") == "1"
    if benchmark:
        (definitions / "test.ldb").write_text(
            "Test.WindowPCRE;Engine:81-255,Target:0;0&1;74695f706372655f74726967676572;0/ti_pcre_trigger:[A-Z]{8}/\n"
        )
    (ROOT / "clamd.conf").write_text(
        "Foreground yes\nLocalSocket /test/clamd.sock\nLocalSocketMode 600\n"
        "DatabaseDirectory /test/defs\nTemporaryDirectory /tmp\n"
        "MaxThreads 4\nMaxQueue 8\n"
        + ("StreamMaxLength 2000M\nMaxFileSize 2000M\nMaxScanSize 4000M\nDisableCache yes\n" if benchmark
           else "StreamMaxLength 8M\nMaxFileSize 8M\nMaxScanSize 16M\n")
        + "AlertExceedsMax yes\nSelfCheck 30\nPCREMaxFileSize 100M\n"
        "AlertEncrypted yes\nAlertBrokenExecutables yes\nAlertBrokenMedia yes\nHeuristicScanPrecedence no\n"
    )


def make_video(name: str, container: str, *, attachment: str | None = None,
               filename="kodi-metadata", mimetype="application/xml") -> Path:
    path = ROOT / name
    command = [
        "ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
        "color=size=32x32:rate=1", "-t", "1", "-threads", "1", "-c:v",
        "msmpeg4v3" if container == "asf" else "ffv1",
    ]
    if attachment:
        command += [
            "-attach", str(ROOT / attachment), "-metadata:s:t", f"filename={filename}",
            "-metadata:s:t", f"mimetype={mimetype}",
        ]
    subprocess.run(command + ["-f", container, str(path)], check=True, timeout=30)
    return path


def run() -> None:
    scanner = ScannerService()
    scanner.settings = scanner.settings.model_copy(update={
        "clamd_socket_path": "/test/clamd.sock",
        "per_job_scan_workers": 4,
        "large_media_enabled": True,
    })
    deadline = time.monotonic() + 60
    while True:
        try:
            version = scanner._clamd_request("VERSION")
            break
        except (OSError, RuntimeError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)
    # Tiny test signatures do not have a FreshClam version/timestamp. Production
    # freshness checks are covered separately; do not download the real database.
    identity = ScannerIdentity(
        backend="clamd", engine_version=version, database_version="test",
        database_updated_at=None, policy_version=scanner.policy_version(), raw_version=version,
    )
    print(f"Testing with {version}", flush=True)
    (ROOT / "clean.xml").write_bytes(b"<movie><title>Fixture</title></movie>")
    (ROOT / "infected.xml").write_bytes(b"<movie><plot>" + b" " * 4096 + EICAR + b"</plot></movie>")
    clean_asf = make_video("asf-with-avi-name.avi", "asf")
    clean_mkv = make_video("kodi-clean.mkv", "matroska", attachment="clean.xml")
    infected_mkv = make_video("kodi-eicar.mkv", "matroska", attachment="infected.xml")
    (ROOT / "hash-only.bin").write_bytes(HASH_ONLY_ATTACHMENT)
    hash_mkv = make_video("hash-only.mkv", "matroska", attachment="hash-only.bin",
                          filename="../../escape.ttf", mimetype="application/x-truetype-font")
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=size=32x32",
                    "-frames:v", "1", "-threads", "1", str(ROOT / "cover.png")], check=True, timeout=30)
    picture_mkv = make_video("picture.mkv", "matroska", attachment="cover.png",
                             filename="cover.png", mimetype="image/png")
    infected_asf = ROOT / "asf-eicar.avi"
    asf_bytes = clean_asf.read_bytes()
    # Put EICAR across a test-window boundary, beyond the original media data.
    boundary = ((len(asf_bytes) // (1024 - 128)) + 2) * (1024 - 128)
    infected_asf.write_bytes(asf_bytes + b"\0" * (boundary - 20 - len(asf_bytes)) + EICAR)

    def check(path: Path, *, infected: bool, method: str, threat="EICAR") -> None:
        result = scanner.scan_path(str(path), identity=identity)
        assert result.infected == infected and result.clean != infected, result
        assert result.scan_method == method, result
        if infected:
            assert threat in (result.threat_name or ""), result
        print(f"PASS {method}: {path.name}: {result.threat_name if infected else 'clean'}", flush=True)

    for path, infected in ((clean_asf, False), (infected_mkv, True)):
        check(path, infected=infected, method="clamd_native")
    for path in (clean_mkv, picture_mkv):
        check(path, infected=False, method="clamd_native_with_attachments")

    descriptor = os.open(hash_mkv, os.O_RDONLY)
    try:
        native = scanner._scan_descriptor(descriptor, str(hash_mkv), file_identity(os.fstat(descriptor)), heartbeat=None, should_stop=None)
        assert not native[0], "fixture must demonstrate an attachment missed by the opaque native scan"
    finally:
        os.close(descriptor)
    check(hash_mkv, infected=True, method="clamd_native_with_attachments", threat="AttachmentHash")

    # Scale only routing/window sizes for tiny fixtures. Real ffprobe, opened
    # descriptors, parallel INSTREAM requests, ClamD and replies remain unmocked.
    settings_type = type(scanner.settings)
    with (
        patch.object(settings_type, "scanner_max_file_bytes", new_callable=PropertyMock, return_value=8192),
        patch.object(settings_type, "large_media_chunk_bytes", new_callable=PropertyMock, return_value=1024),
        patch.object(settings_type, "large_media_min_chunk_bytes", new_callable=PropertyMock, return_value=256),
        patch.object(settings_type, "large_media_overlap_bytes", new_callable=PropertyMock, return_value=128),
    ):
        for path in (clean_asf, clean_mkv, infected_mkv, infected_asf, hash_mkv, picture_mkv):
            if path.stat().st_size <= 8192:
                with path.open("ab") as output:
                    output.write(b"\0" * (8193 - path.stat().st_size))
        for path, infected in (
            (clean_asf, False), (clean_mkv, False),
            (infected_asf, True), (infected_mkv, True),
        ):
            check(path, infected=infected, method="media_windows_and_attachments")
        check(hash_mkv, infected=True, method="media_windows_and_attachments", threat="AttachmentHash")
        check(picture_mkv, infected=False, method="media_windows_and_attachments")

        (ROOT / "not-video.mkv").write_bytes(b"not a video\n" * 1000)
        try:
            scanner.scan_path(str(ROOT / "not-video.mkv"), identity=identity)
        except ScannerPolicyError:
            print("PASS malformed media stays blocked", flush=True)
        else:
            raise AssertionError("malformed media received a verdict")

    # A ZIP marked encrypted must be held, never treated as malware or clean.
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as writer:
        writer.writestr("payload.txt", b"harmless fixture data" * 10)
    marked = bytearray(archive.getvalue())
    marked[6] |= 1
    marked[marked.index(b"PK\x01\x02") + 8] |= 1
    encrypted_path = ROOT / "encrypted.zip"
    encrypted_path.write_bytes(marked)
    try:
        scanner.scan_path(str(encrypted_path), identity=identity)
    except ScannerPolicyError as error:
        assert "Encrypted" in str(error), error
        print("PASS encrypted archive is held, not an infection", flush=True)
    else:
        raise AssertionError("encrypted archive received a verdict")
    assert not list(Path("/tmp").glob("ti-attachment-*")), "temporary attachments leaked"
    if os.environ.get("TI_TEST_BENCHMARK_WINDOWS") == "1":
        benchmark_windows(scanner, clean_asf)


def benchmark_windows(scanner: ScannerService, source: Path) -> None:
    """Synthetic coverage/timing comparison, not a production throughput claim."""
    sample = ROOT / "window-benchmark.avi"
    with sample.open("wb") as output:
        output.write(source.read_bytes())
        output.seek(128 * 1024 * 1024 - 128)
        output.write(b"ti_pcre_trigger:ABCDEFGH" + b"\0" * 105)
    descriptor = os.open(sample, os.O_RDONLY)
    try:
        expected = file_identity(os.fstat(descriptor))
        for window in (512, 32):
            scanner.settings.large_media_chunk_mib = window
            scanner.settings.large_media_min_chunk_mib = min(64, window // 2)
            started = time.monotonic()
            infected, threat, _ = scanner._scan_large_media_descriptor(
                descriptor, str(sample), expected, heartbeat=None, should_stop=None,
            )
            print(f"BENCHMARK synthetic=128MiB window={window}MiB elapsed={time.monotonic()-started:.3f}s pcre_detected={infected}", flush=True)
            assert infected == (window == 32), (window, infected, threat)
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    assert os.getuid() != 0, "integration tests must run non-root"
    if sys.argv[1:] == ["prepare"]:
        prepare()
    elif sys.argv[1:] == ["scan"]:
        run()
    else:
        raise SystemExit("usage: integration_media.py prepare|scan")
