from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import stat
import struct
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .config import get_settings

VERSION_PATTERN = re.compile(r"ClamAV\s+([^/\s]+)/([^/\s]+)/([^\r\n]+)", re.IGNORECASE)
LIMIT_DETECTION_MARKERS = (
    "heuristics.limits.exceeded",
    "size limit exceeded",
    "scan limit exceeded",
    "limits exceeded",
    "stream size limit exceeded",
)
STREAM_CHUNK_BYTES = 1024 * 1024
MAX_REPLY_BYTES = 1024 * 1024
MAX_FFPROBE_OUTPUT_BYTES = 1024 * 1024
FileIdentity = tuple[int, int, int, int, int]

LARGE_MEDIA_FORMATS = frozenset(
    {
        "avi",
        "matroska",
        "mov",
        "mp4",
        "mpeg",
        "mpegts",
        "ogg",
        "webm",
    }
)
LARGE_MEDIA_STREAM_TYPES = frozenset({"audio", "attachment", "subtitle", "video"})
SAFE_ATTACHMENT_SUFFIXES = frozenset(
    {
        ".ass",
        ".gif",
        ".jpeg",
        ".jpg",
        ".nfo",
        ".otf",
        ".png",
        ".srt",
        ".ssa",
        ".ttf",
        ".txt",
        ".webp",
        ".woff",
        ".woff2",
    }
)


@dataclass(frozen=True)
class ScannerIdentity:
    backend: str
    engine_version: str | None
    database_version: str | None
    database_updated_at: datetime | None
    policy_version: str
    raw_version: str


@dataclass(frozen=True)
class ScannerHealth:
    status: str
    can_scan: bool
    message: str
    checked_at: datetime
    identity: ScannerIdentity | None = None
    definitions_age_hours: float | None = None

    def as_dict(self) -> dict[str, object]:
        identity = self.identity
        return {
            "status": self.status,
            "can_scan": self.can_scan,
            "message": self.message,
            "checked_at": self.checked_at,
            "backend": identity.backend if identity else None,
            "engine_version": identity.engine_version if identity else None,
            "database_version": identity.database_version if identity else None,
            "database_updated_at": identity.database_updated_at if identity else None,
            "definitions_age_hours": self.definitions_age_hours,
            "policy_version": identity.policy_version if identity else None,
            "raw_version": identity.raw_version if identity else None,
        }


@dataclass(frozen=True)
class ScanResult:
    clean: bool
    infected: bool
    identity: ScannerIdentity
    scan_started_at: datetime
    duration_seconds: float
    threat_name: str | None = None
    raw_output: str = ""
    scan_method: str = "clamd_native"


class ScanInterrupted(RuntimeError):
    pass


class ScannerUnavailable(RuntimeError):
    pass


class ScannerDefinitionsStale(RuntimeError):
    pass


class ScannerPolicyError(RuntimeError):
    pass


class ScannerLimitError(ScannerPolicyError):
    """ClamD reached a configured inspection limit without a clean verdict."""


def parse_scanner_version(raw_output: str) -> tuple[str | None, str | None, datetime | None]:
    raw_output = (raw_output or "").strip().strip("\0")
    match = VERSION_PATTERN.search(raw_output)
    if not match:
        return None, None, None
    engine_version, database_version, database_date = match.groups()
    parsed_date: datetime | None = None
    try:
        parsed_date = parsedate_to_datetime(database_date.strip())
    except (TypeError, ValueError, OverflowError):
        for date_format in ("%a %b %d %H:%M:%S %Y", "%b %d %H:%M:%S %Y"):
            try:
                parsed_date = datetime.strptime(database_date.strip(), date_format)
                break
            except ValueError:
                continue
    if parsed_date and parsed_date.tzinfo is not None:
        parsed_date = parsed_date.astimezone(timezone.utc).replace(tzinfo=None)
    return engine_version, database_version, parsed_date


def parse_scan_response(response: str) -> tuple[bool, str | None]:
    response = response.strip().strip("\0")
    if not response:
        raise RuntimeError("scanner returned an empty response")
    if any(marker in response.casefold() for marker in LIMIT_DETECTION_MARKERS):
        raise ScannerLimitError(
            "ClamAV could not fully inspect this file because a configured limit was exceeded: "
            f"{response[:500]}"
        )
    if response.endswith(": OK") or response == "OK":
        return False, None
    if response.endswith(" FOUND"):
        threat_name = response.rsplit(": ", 1)[-1].removesuffix(" FOUND").strip() or "unknown"
        return True, threat_name
    if response.endswith(" ERROR"):
        raise RuntimeError(f"scanner could not inspect the file: {response}")
    raise RuntimeError(f"unexpected scanner response: {response}")


def file_identity(info: os.stat_result) -> FileIdentity:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def large_media_window_ranges(
    total_bytes: int,
    window_bytes: int,
    overlap_bytes: int,
) -> list[tuple[int, int]]:
    if total_bytes < 0:
        raise ValueError("total byte count cannot be negative")
    if window_bytes <= 0:
        raise ValueError("large-media window must be positive")
    if overlap_bytes < 0 or overlap_bytes >= window_bytes:
        raise ValueError("large-media overlap must be smaller than its window")
    if total_bytes == 0:
        return [(0, 0)]

    ranges: list[tuple[int, int]] = []
    offset = 0
    step = window_bytes - overlap_bytes
    while offset < total_bytes:
        length = min(window_bytes, total_bytes - offset)
        ranges.append((offset, length))
        if offset + length >= total_bytes:
            break
        offset += step
    return ranges


def parse_large_media_probe(raw_output: str, path: str) -> str:
    try:
        payload = json.loads(raw_output)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ScannerPolicyError(
            f"oversized file is not a valid supported video container: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise ScannerPolicyError(f"ffprobe returned an invalid media description: {path}")

    format_payload = payload.get("format")
    format_name = format_payload.get("format_name") if isinstance(format_payload, dict) else None
    detected_formats = {
        part.strip().casefold()
        for part in str(format_name or "").split(",")
        if part.strip()
    }
    approved_formats = detected_formats & LARGE_MEDIA_FORMATS
    if not approved_formats:
        detected = ",".join(sorted(detected_formats)) or "unknown"
        raise ScannerPolicyError(
            f"oversized content type is not an approved video container ({detected}): {path}"
        )

    streams = payload.get("streams")
    if not isinstance(streams, list) or len(streams) > 1024:
        raise ScannerPolicyError(f"oversized media has an invalid or excessive stream table: {path}")
    video_streams = 0
    attachment_streams = 0
    for stream in streams:
        if not isinstance(stream, dict):
            raise ScannerPolicyError(f"oversized media has a malformed stream entry: {path}")
        stream_type = str(stream.get("codec_type") or "").casefold()
        if stream_type not in LARGE_MEDIA_STREAM_TYPES:
            raise ScannerPolicyError(
                f"oversized media contains unsupported stream type {stream_type or 'unknown'}: {path}"
            )
        if stream_type == "video":
            video_streams += 1
        if stream_type == "attachment":
            attachment_streams += 1
            if attachment_streams > 64:
                raise ScannerPolicyError(f"oversized media contains too many attachments: {path}")
            tags = stream.get("tags")
            filename = tags.get("filename") if isinstance(tags, dict) else None
            suffix = os.path.splitext(str(filename or ""))[1].casefold()
            if suffix not in SAFE_ATTACHMENT_SUFFIXES:
                raise ScannerPolicyError(
                    "oversized media contains an attachment that is not a recognized font, "
                    f"image, subtitle, or text file ({filename or 'unnamed'}): {path}"
                )
    if video_streams == 0:
        raise ScannerPolicyError(f"oversized container does not contain a video stream: {path}")
    return ",".join(sorted(approved_formats))


class ScannerService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._health_lock = threading.Lock()
        self._cached_health: ScannerHealth | None = None
        self._cached_health_at = 0.0

    def policy_version(self) -> str:
        policy = {
            "backend": "clamd-instream",
            "max_file_bytes": self.settings.scanner_max_file_bytes,
            "large_media_enabled": self.settings.large_media_enabled,
            "large_media_max_file_bytes": self.settings.large_media_max_file_bytes,
            "large_media_chunk_bytes": self.settings.large_media_chunk_bytes,
            "large_media_overlap_bytes": self.settings.large_media_overlap_bytes,
            "policy_version": self.settings.scanner_policy_version,
        }
        fingerprint = hashlib.sha256(
            json.dumps(policy, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:12]
        return f"{self.settings.scanner_policy_version}:{fingerprint}"

    def scanner_version(self) -> str | None:
        health = self.health()
        return health.identity.raw_version if health.identity else None

    def health(self, *, force: bool = False) -> ScannerHealth:
        max_age = max(self.settings.scanner_health_cache_seconds, 1)
        with self._health_lock:
            if (
                not force
                and self._cached_health is not None
                and time.monotonic() - self._cached_health_at < max_age
            ):
                return self._cached_health
            health = self._load_health()
            self._cached_health = health
            self._cached_health_at = time.monotonic()
            return health

    def clear_health_cache(self) -> None:
        with self._health_lock:
            self._cached_health = None
            self._cached_health_at = 0.0

    def require_healthy(self, *, force: bool = False) -> ScannerIdentity:
        health = self.health(force=force)
        if health.can_scan and health.identity is not None:
            return health.identity
        if health.status == "stale":
            raise ScannerDefinitionsStale(health.message)
        raise ScannerUnavailable(health.message)

    def scan_path(
        self,
        path: str,
        *,
        identity: ScannerIdentity | None = None,
        heartbeat: Callable[[], bool] | None = None,
        should_stop: Callable[[], bool] | None = None,
        expected_file_identity: FileIdentity | None = None,
    ) -> ScanResult:
        identity = identity or self.require_healthy(force=True)
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise RuntimeError(f"scan file is unavailable: {path}: {exc}") from exc
        try:
            initial_stat = os.fstat(descriptor)
            if not stat.S_ISREG(initial_stat.st_mode):
                raise ScannerPolicyError(f"refusing to scan a non-regular file: {path}")
            expected = file_identity(initial_stat)
            if expected_file_identity is not None and expected != expected_file_identity:
                raise RuntimeError(f"scan file identity changed before ClamD received it: {path}")
            if should_stop and should_stop():
                raise ScanInterrupted("scan interrupted before the current file started")
            started_at = datetime.utcnow()
            started = time.monotonic()
            if initial_stat.st_size <= self.settings.scanner_max_file_bytes:
                try:
                    infected, threat_name, output = self._scan_descriptor(
                        descriptor,
                        path,
                        expected,
                        heartbeat=heartbeat,
                        should_stop=should_stop,
                    )
                    scan_method = "clamd_native"
                except ScannerLimitError as native_limit:
                    # MaxScanSize accounts for parser/expanded content, so a
                    # file below the raw native-size boundary can still reach
                    # it. Retry only through the media route: that route first
                    # verifies the real container and rejects archives or
                    # unknown formats before using bounded ClamD windows.
                    try:
                        infected, threat_name, output = self._scan_large_media_descriptor(
                            descriptor,
                            path,
                            expected,
                            heartbeat=heartbeat,
                            should_stop=should_stop,
                        )
                    except ScannerPolicyError as fallback_error:
                        raise ScannerPolicyError(
                            f"{native_limit}; verified-media fallback was rejected: {fallback_error}"
                        ) from fallback_error
                    output = (
                        f"native-limit fallback ({native_limit}); {output}"
                    )[:MAX_REPLY_BYTES]
                    scan_method = "large_media_full_byte_windows"
            else:
                infected, threat_name, output = self._scan_large_media_descriptor(
                    descriptor,
                    path,
                    expected,
                    heartbeat=heartbeat,
                    should_stop=should_stop,
                )
                scan_method = "large_media_full_byte_windows"
            return ScanResult(
                clean=not infected,
                infected=infected,
                identity=identity,
                scan_started_at=started_at,
                duration_seconds=max(time.monotonic() - started, 0.0),
                threat_name=threat_name,
                raw_output=output,
                scan_method=scan_method,
            )
        finally:
            os.close(descriptor)

    def _load_health(self) -> ScannerHealth:
        checked_at = datetime.utcnow()
        try:
            self._validate_policy_configuration()
        except ScannerPolicyError as exc:
            return ScannerHealth(
                status="unavailable",
                can_scan=False,
                message=f"ClamAV scan policy is invalid: {exc}",
                checked_at=checked_at,
            )
        try:
            raw_version = self._version_output().strip()
        except (OSError, RuntimeError) as exc:
            return ScannerHealth(
                status="unavailable",
                can_scan=False,
                message=f"ClamAV is unavailable: {exc}",
                checked_at=checked_at,
            )
        engine_version, database_version, database_updated_at = parse_scanner_version(raw_version)
        identity = ScannerIdentity(
            backend="clamd-instream",
            engine_version=engine_version,
            database_version=database_version,
            database_updated_at=database_updated_at,
            policy_version=self.policy_version(),
            raw_version=raw_version[:255],
        )
        if not engine_version or database_updated_at is None:
            return ScannerHealth(
                status="unavailable",
                can_scan=False,
                message="ClamAV version or signature freshness could not be verified; scans are blocked.",
                checked_at=checked_at,
                identity=identity,
            )
        definitions_age_hours = max((checked_at - database_updated_at).total_seconds() / 3600, 0.0)
        stale_hours = max(self.settings.scanner_definitions_stale_hours, 1)
        warning_hours = min(max(self.settings.scanner_definitions_warn_hours, 1), stale_hours)
        if definitions_age_hours >= stale_hours:
            return ScannerHealth(
                status="stale",
                can_scan=False,
                message=(
                    f"ClamAV definitions are {definitions_age_hours:.1f} hours old; "
                    f"new scans are blocked at {stale_hours} hours."
                ),
                checked_at=checked_at,
                identity=identity,
                definitions_age_hours=definitions_age_hours,
            )
        status = "warning" if definitions_age_hours >= warning_hours else "healthy"
        message = (
            f"ClamAV definitions are {definitions_age_hours:.1f} hours old."
            if status == "warning"
            else "ClamAV daemon and definitions are healthy."
        )
        return ScannerHealth(
            status=status,
            can_scan=True,
            message=message,
            checked_at=checked_at,
            identity=identity,
            definitions_age_hours=definitions_age_hours,
        )

    def _validate_policy_configuration(self) -> None:
        native_bytes = self.settings.scanner_max_file_bytes
        if not 1 <= native_bytes <= 2000 * 1024 * 1024:
            raise ScannerPolicyError("TI_SCANNER_MAX_FILE_MIB must be between 1 and 2000")
        if self.settings.large_media_max_file_bytes <= 0:
            raise ScannerPolicyError("TI_LARGE_MEDIA_MAX_FILE_GIB must be positive")
        window_bytes = self.settings.large_media_chunk_bytes
        overlap_bytes = self.settings.large_media_overlap_bytes
        if not 1 <= window_bytes <= native_bytes:
            raise ScannerPolicyError(
                "TI_LARGE_MEDIA_CHUNK_MIB must be positive and no larger than "
                "TI_SCANNER_MAX_FILE_MIB"
            )
        if overlap_bytes < 0 or overlap_bytes >= window_bytes:
            raise ScannerPolicyError(
                "TI_LARGE_MEDIA_OVERLAP_KIB must be nonnegative and smaller than the window"
            )
        if self.settings.large_media_enabled and (
            not os.path.isabs(self.settings.ffprobe_binary)
            or not os.access(self.settings.ffprobe_binary, os.X_OK)
        ):
            raise ScannerPolicyError(
                f"TI_FFPROBE_BINARY is not an executable absolute path: "
                f"{self.settings.ffprobe_binary}"
            )

    def _version_output(self) -> str:
        return self._clamd_request("VERSION")

    def _clamd_request(self, command: str) -> str:
        socket_path = self.settings.clamd_socket_path
        timeout = max(self.settings.scanner_connect_timeout_seconds, 1)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(timeout)
                client.connect(socket_path)
                client.sendall(b"z" + command.encode("ascii") + b"\0")
                raw = self._receive_reply(client)
        except OSError as exc:
            raise ScannerUnavailable(f"cannot connect to clamd socket {socket_path}: {exc}") from exc
        return raw.decode("utf-8", errors="replace")

    def _scan_descriptor(
        self,
        descriptor: int,
        path: str,
        expected: FileIdentity,
        *,
        heartbeat: Callable[[], bool] | None,
        should_stop: Callable[[], bool] | None,
    ) -> tuple[bool, str | None, str]:
        timeout = max(int(self.settings.scanner_scan_timeout_seconds), 60)
        deadline = time.monotonic() + timeout
        raw_reply = self._scan_descriptor_window(
            descriptor,
            offset=0,
            length=expected[2],
            deadline=deadline,
            timeout_description=f"configured {timeout}-second timeout",
            heartbeat=heartbeat,
            should_stop=should_stop,
        )
        self._verify_file_identity(descriptor, path, expected)
        output = raw_reply.decode("utf-8", errors="replace")
        infected, threat_name = parse_scan_response(output)
        return infected, threat_name, output

    def _scan_large_media_descriptor(
        self,
        descriptor: int,
        path: str,
        expected: FileIdentity,
        *,
        heartbeat: Callable[[], bool] | None,
        should_stop: Callable[[], bool] | None,
    ) -> tuple[bool, str | None, str]:
        if not self.settings.large_media_enabled:
            raise ScannerPolicyError(
                f"file exceeds the native ClamAV limit and the large-media policy is disabled: {path}"
            )
        if expected[2] > self.settings.large_media_max_file_bytes:
            raise ScannerPolicyError(
                f"file is {expected[2]} bytes, above the bounded large-media ceiling of "
                f"{self.settings.large_media_max_file_bytes} bytes: {path}"
            )
        window_bytes = self.settings.large_media_chunk_bytes
        overlap_bytes = self.settings.large_media_overlap_bytes
        if window_bytes > self.settings.scanner_max_file_bytes:
            raise ScannerPolicyError(
                "large-media window exceeds the configured native ClamD stream ceiling"
            )
        try:
            ranges = large_media_window_ranges(expected[2], window_bytes, overlap_bytes)
        except ValueError as exc:
            raise ScannerPolicyError(f"invalid large-media window configuration: {exc}") from exc

        timeout = max(int(self.settings.large_media_scan_timeout_seconds), 60)
        deadline = time.monotonic() + timeout
        media_format = self._probe_large_media_descriptor(descriptor, path, deadline=deadline)
        self._verify_file_identity(descriptor, path, expected)

        replies: list[str] = []
        for index, (offset, length) in enumerate(ranges, start=1):
            if time.monotonic() >= deadline:
                self.clear_health_cache()
                raise ScannerUnavailable(
                    f"large-media scan exceeded the configured {timeout}-second timeout"
                )
            raw_reply = self._scan_descriptor_window(
                descriptor,
                offset=offset,
                length=length,
                deadline=deadline,
                timeout_description=f"large-media {timeout}-second timeout",
                heartbeat=heartbeat,
                should_stop=should_stop,
            )
            self._verify_file_identity(descriptor, path, expected)
            output = raw_reply.decode("utf-8", errors="replace")
            infected, threat_name = parse_scan_response(output)
            replies.append(
                f"window={index}/{len(ranges)} offset={offset} length={length} {output[:500]}"
            )
            if infected:
                return True, threat_name, replies[-1]
        return False, None, (
            f"large-media format={media_format} windows={len(ranges)} coverage=all-bytes; "
            + "; ".join(replies)
        )[:MAX_REPLY_BYTES]

    def _probe_large_media_descriptor(
        self,
        descriptor: int,
        path: str,
        *,
        deadline: float | None = None,
    ) -> str:
        command = [
            self.settings.ffprobe_binary,
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-show_entries",
            "format=format_name:stream=index,codec_type,codec_name:stream_tags=filename,mimetype",
            "-of",
            "json",
            f"/proc/self/fd/{descriptor}",
        ]
        probe_timeout = max(float(self.settings.large_media_probe_timeout_seconds), 1.0)
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ScannerPolicyError(f"oversized media validation timed out: {path}")
            probe_timeout = min(probe_timeout, remaining)
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=probe_timeout,
                check=False,
                pass_fds=(descriptor,),
            )
        except FileNotFoundError as exc:
            raise ScannerUnavailable(
                f"large-media validation is unavailable because ffprobe was not found: {exc}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ScannerPolicyError(f"oversized media validation timed out: {path}") from exc
        if len(completed.stdout.encode("utf-8", "replace")) > MAX_FFPROBE_OUTPUT_BYTES:
            raise ScannerPolicyError(f"oversized media has an excessive stream description: {path}")
        if completed.returncode != 0:
            detail = " ".join(completed.stderr.strip().split())[:500]
            raise ScannerPolicyError(
                f"oversized file failed media-container validation{': ' + detail if detail else ''}: {path}"
            )
        return parse_large_media_probe(completed.stdout, path)

    def _scan_descriptor_window(
        self,
        descriptor: int,
        *,
        offset: int,
        length: int,
        deadline: float,
        timeout_description: str,
        heartbeat: Callable[[], bool] | None,
        should_stop: Callable[[], bool] | None,
    ) -> bytes:
        socket_path = self.settings.clamd_socket_path
        interval = min(max(int(self.settings.scan_heartbeat_seconds), 1), 30)

        def check_deadline() -> None:
            if time.monotonic() >= deadline:
                self.clear_health_cache()
                raise ScannerUnavailable(
                    f"ClamD scan exceeded the {timeout_description}"
                )

        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(max(self.settings.scanner_connect_timeout_seconds, 1))
                client.connect(socket_path)
                client.sendall(b"zINSTREAM\0")
                client.settimeout(interval)
                current_offset = offset
                remaining = length
                last_heartbeat = time.monotonic()
                while remaining > 0:
                    check_deadline()
                    if should_stop and should_stop():
                        raise ScanInterrupted("scan interrupted; the current file will be retried")
                    chunk = os.pread(
                        descriptor,
                        min(STREAM_CHUNK_BYTES, remaining),
                        current_offset,
                    )
                    if not chunk:
                        raise RuntimeError("scan file became shorter while ClamD received it")
                    client.sendall(struct.pack("!I", len(chunk)) + chunk)
                    current_offset += len(chunk)
                    remaining -= len(chunk)
                    if time.monotonic() - last_heartbeat >= interval:
                        if heartbeat and not heartbeat():
                            raise ScanInterrupted("scan lease was lost; the current file will be retried")
                        last_heartbeat = time.monotonic()
                client.sendall(struct.pack("!I", 0))

                def on_timeout() -> None:
                    check_deadline()
                    if (should_stop and should_stop()) or (heartbeat and not heartbeat()):
                        raise ScanInterrupted("scan interrupted; the current file will be retried")

                raw_reply = self._receive_reply(client, on_timeout=on_timeout)
        except ScanInterrupted:
            raise
        except OSError as exc:
            self.clear_health_cache()
            raise ScannerUnavailable(f"lost connection to clamd socket {socket_path}: {exc}") from exc
        return raw_reply

    @staticmethod
    def _receive_reply(
        client: socket.socket,
        on_timeout: Callable[[], None] | None = None,
    ) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            try:
                chunk = client.recv(4096)
            except socket.timeout:
                if on_timeout is None:
                    raise
                on_timeout()
                continue
            if not chunk:
                raise RuntimeError("clamd closed the connection without a complete reply")
            terminator = chunk.find(b"\0")
            selected = chunk if terminator < 0 else chunk[:terminator]
            chunks.append(selected)
            total += len(selected)
            if total > MAX_REPLY_BYTES:
                raise RuntimeError("clamd returned an oversized reply")
            if terminator >= 0:
                return b"".join(chunks)

    @staticmethod
    def _verify_file_identity(descriptor: int, path: str, expected: FileIdentity) -> None:
        descriptor_info = os.fstat(descriptor)
        try:
            path_info = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise RuntimeError(f"scan file vanished or was replaced while ClamD scanned it: {path}") from exc
        if (
            not stat.S_ISREG(path_info.st_mode)
            or file_identity(descriptor_info) != expected
            or file_identity(path_info) != expected
        ):
            raise RuntimeError(f"scan file identity changed while ClamD scanned it: {path}")
