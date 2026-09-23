from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import stat
import struct
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from subprocess import CompletedProcess

from .config import get_settings
from .media_tools import MAX_SINGLE_ALLOCATION_BYTES, MAX_STDOUT_BYTES, MediaToolError, run_media_tool

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
FileIdentity = tuple[int, int, int, int, int]
SCANNER_IMPLEMENTATION_POLICY = "bounded-media-attachments-v1"

LARGE_VIDEO_FORMATS = frozenset(
    {
        "asf",
        "avi",
        "flv",
        "matroska",
        "mov",
        "mp4",
        "mpeg",
        "mpegts",
        "ogg",
        "webm",
    }
)
LARGE_TRUEHD_FORMAT = "truehd"
LARGE_TRUEHD_SUFFIXES = frozenset({".thd", ".truehd"})
LARGE_MEDIA_STREAM_TYPES = frozenset({"audio", "attachment", "subtitle", "video"})
MAX_CHAPTER_SAMPLES = 4096
KODI_METADATA_FILENAMES = frozenset({"kodi-metadata", "kodi-override-metadata"})
KODI_METADATA_MIMETYPES = frozenset({"application/xml", "text/xml", "text/plain"})
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
        ".ttc",
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


@dataclass(frozen=True)
class _WindowScanOutcome:
    infected: bool
    threat_name: str | None
    replies: tuple[str, ...]


@dataclass(frozen=True)
class MediaAttachment:
    index: int
    filename: str
    size_bytes: int | None
    is_picture: bool = False
    # MP4 chapter text is stored in packets, not attachment extradata.
    chapter_samples: int | None = None


@dataclass(frozen=True)
class MediaProbe:
    format_name: str
    attachments: tuple[MediaAttachment, ...] = ()


class ScanInterrupted(RuntimeError):
    pass


class _ParallelWindowCancelled(ScanInterrupted):
    """A sibling window reached a terminal result, so this work is no longer needed."""


class ScannerUnavailable(RuntimeError):
    pass


class ScannerDefinitionsStale(RuntimeError):
    pass


class ScannerPolicyError(RuntimeError):
    pass


class ScannerLimitError(ScannerPolicyError):
    """ClamD reached a configured inspection limit without a clean verdict."""

    def __init__(self, message: str, *, limit_name: str = "unknown") -> None:
        super().__init__(message)
        self.limit_name = limit_name

    @property
    def can_subdivide(self) -> bool:
        return self.limit_name in {"maxfilesize", "streammaxlength"}

    @property
    def can_use_media_fallback(self) -> bool:
        return self.can_subdivide or self.limit_name == "maxscansize"


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
        match = re.search(r"Heuristics\.Limits\.Exceeded\.(\w+)", response, re.IGNORECASE)
        limit_name = match.group(1).casefold() if match else "unknown"
        if "instream size limit exceeded" in response.casefold():
            limit_name = "streammaxlength"
        raise ScannerLimitError(
            "ClamAV could not fully inspect this file because a configured limit was exceeded: "
            f"{response[:500]}", limit_name=limit_name,
        )
    if response.endswith(": OK") or response == "OK":
        return False, None
    if response.endswith(" FOUND"):
        threat_name = response.rsplit(": ", 1)[-1].removesuffix(" FOUND").strip() or "unknown"
        if threat_name.casefold().startswith((
            "heuristics.encrypted.", "heuristics.broken.", "broken.executable", "broken.media",
        )):
            raise ScannerPolicyError(
                f"ClamAV reported encrypted or malformed content requiring review, not a malware verdict: {threat_name}"
            )
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


def split_large_media_window(
    offset: int,
    length: int,
    minimum_bytes: int,
    overlap_bytes: int,
) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """Split a limited window into two overlapping windows without dropping bytes."""
    if offset < 0 or length <= 0 or minimum_bytes <= 0:
        raise ValueError("window offset, length, and minimum must be valid positive values")
    if overlap_bytes < 0 or overlap_bytes >= minimum_bytes:
        raise ValueError("adaptive overlap must be smaller than the minimum window")

    left_length = (length + overlap_bytes) // 2
    right_offset = offset + left_length - overlap_bytes
    right_length = offset + length - right_offset
    if (
        left_length < minimum_bytes
        or right_length < minimum_bytes
        or left_length >= length
        or right_length >= length
    ):
        return None
    return (offset, left_length), (right_offset, right_length)


def _media_policy_error(
    reason: str,
    path: str,
    *,
    container=None,
    stream: dict | None = None,
    **details,
) -> ScannerPolicyError:
    """Diagnostic metadata only: bounded, escaped values, never a payload dump."""
    fields = {"path": path, "container": container}
    if stream is not None:
        tags = stream.get("tags")
        tags = tags if isinstance(tags, dict) else {}
        fields.update(
            stream_index=stream.get("index"), stream_type=stream.get("codec_type"),
            codec=stream.get("codec_name"), codec_tag=stream.get("codec_tag_string"),
            handler=tags.get("handler_name"),
        )
        if stream.get("codec_type") == "attachment" or "filename" in tags:
            filename = tags.get("filename")
            fields.update(
                attachment=filename,
                extension=os.path.splitext(filename)[1].casefold() if isinstance(filename, str) else None,
                mime=tags.get("mimetype"), declared_bytes=stream.get("extradata_size"),
            )
    fields.update(details)

    def display(key, value):
        if value is None or value == "":
            value = "unknown"
        elif not isinstance(value, (str, int, float, bool)):
            value = f"invalid {type(value).__name__}"
        if isinstance(value, str):
            limit = 4096 if key == "path" else 256
            if len(value) > limit:
                value = value[:limit] + "...[truncated]"
        return json.dumps(value, ensure_ascii=True)

    context = "; ".join(f"{key}={display(key, value)}" for key, value in fields.items())
    return ScannerPolicyError(f"{reason}; {context}")


def parse_large_media_probe(
    raw_output: str,
    path: str,
    *,
    require_video: bool = True,
    attachment_max_bytes: int = 16 * 1024 * 1024,
    attachment_total_bytes: int = 64 * 1024 * 1024,
) -> MediaProbe:
    try:
        payload = json.loads(raw_output)
    except (TypeError, json.JSONDecodeError) as exc:
        raise _media_policy_error("ffprobe returned invalid media JSON", path) from exc
    if not isinstance(payload, dict):
        raise _media_policy_error("ffprobe returned an invalid media description", path)

    format_payload = payload.get("format")
    format_name = (
        format_payload.get("format_name") if isinstance(format_payload, dict) else None
    )
    detected_formats = {
        part.strip().casefold()
        for part in str(format_name or "").split(",")
        if part.strip()
    }

    def rejected(reason: str, stream: dict | None = None, **details) -> ScannerPolicyError:
        return _media_policy_error(reason, path, container=format_name, stream=stream, **details)

    approved_video_formats = detected_formats & LARGE_VIDEO_FORMATS
    is_raw_truehd = detected_formats == {LARGE_TRUEHD_FORMAT}
    if not approved_video_formats and not is_raw_truehd:
        raise rejected(
            "content type is not an approved video container or raw TrueHD audio stream"
        )

    streams = payload.get("streams")
    if not isinstance(streams, list) or len(streams) > 1024:
        raise rejected(
            "media has an invalid or excessive stream table",
            stream_count=len(streams) if isinstance(streams, list) else None, max_streams=1024,
        )
    if is_raw_truehd:
        suffix = os.path.splitext(path)[1].casefold()
        if suffix not in LARGE_TRUEHD_SUFFIXES:
            raise rejected("raw TrueHD content requires a .thd or .truehd filename", extension=suffix)
        if len(streams) != 1 or not isinstance(streams[0], dict):
            raise rejected(
                "raw TrueHD content must contain exactly one TrueHD audio stream",
                stream_count=len(streams),
            )
        stream_type = str(streams[0].get("codec_type") or "").casefold()
        codec_name = str(streams[0].get("codec_name") or "").casefold()
        if stream_type != "audio" or codec_name != LARGE_TRUEHD_FORMAT:
            raise rejected("raw TrueHD content must contain exactly one TrueHD audio stream", streams[0])
        return MediaProbe(LARGE_TRUEHD_FORMAT)

    video_streams = 0
    attachments: list[MediaAttachment] = []
    reserved_attachment_bytes = 0
    attachment_indices: set[int] = set()
    for position, stream in enumerate(streams):
        if not isinstance(stream, dict):
            raise rejected("media has a malformed stream entry", stream_position=position)
        stream_type = str(stream.get("codec_type") or "").casefold()
        is_chapter_text = (
            bool(approved_video_formats & {"mov", "mp4"})
            and stream_type == "data"
            and stream.get("codec_name") == "bin_data"
            and stream.get("codec_tag_string") == "text"
        )
        if stream_type not in LARGE_MEDIA_STREAM_TYPES and not is_chapter_text:
            raise rejected(
                "media contains unsupported stream type "
                "(only video, audio, subtitle, attachment and validated MP4 chapter text are supported)", stream,
            )
        disposition = stream.get("disposition")
        is_picture = isinstance(disposition, dict) and disposition.get("attached_pic") == 1
        if is_picture and stream_type != "video":
            raise rejected("media has an invalid attached-picture stream", stream)
        if stream_type == "video" and not is_picture:
            video_streams += 1
        if stream_type == "attachment" or is_picture or is_chapter_text:
            if len(attachments) >= 64:
                raise rejected(
                    "media contains too many attachments", stream,
                    attachment_count=len(attachments) + 1, max_attachments=64,
                )
            tags = stream.get("tags")
            filename = tags.get("filename") if isinstance(tags, dict) else None
            mimetype = tags.get("mimetype") if isinstance(tags, dict) else None
            suffix = os.path.splitext(str(filename or ""))[1].casefold()
            is_kodi_metadata = (
                "matroska" in approved_video_formats
                and str(filename or "") in KODI_METADATA_FILENAMES
                and str(mimetype or "").split(";", 1)[0].strip().casefold()
                in KODI_METADATA_MIMETYPES
            )
            if not is_chapter_text and suffix not in SAFE_ATTACHMENT_SUFFIXES and not is_kodi_metadata:
                raise rejected(
                    "unsupported attachment: expected a recognized font, image, subtitle, "
                    "text file, or named Kodi text/XML metadata", stream,
                )
            index = stream.get("index")
            if type(index) is not int or index < 0 or index in attachment_indices:
                raise rejected("media attachment has an invalid or duplicate stream index", stream)
            attachment_indices.add(index)
            size = stream.get("extradata_size")
            chapter_samples = None
            if is_chapter_text:
                try:
                    chapter_samples = _media_integer(stream.get("nb_frames"))
                except ValueError:
                    chapter_samples = 0
                if not 0 < chapter_samples <= MAX_CHAPTER_SAMPLES:
                    raise rejected(
                        "MP4 chapter track has a missing or excessive sample count", stream,
                        declared_samples=stream.get("nb_frames"), max_samples=MAX_CHAPTER_SAMPLES,
                    )
                filename = f"chapter-track-{index}.text"
            if is_picture or is_chapter_text:
                # Unknown-size packet payloads reserve the full per-object
                # allowance before starting extraction. Never trust a title/MIME.
                size = None
                reserved_attachment_bytes += attachment_max_bytes
            else:
                if type(size) is not int or not 0 < size <= attachment_max_bytes:
                    raise rejected(
                        "media attachment has a missing, empty, or excessive size", stream,
                        min_attachment_bytes=1, max_attachment_bytes=attachment_max_bytes,
                    )
                reserved_attachment_bytes += size
            if reserved_attachment_bytes > attachment_total_bytes:
                raise rejected(
                    "media attachments exceed the total extraction budget", stream,
                    reserved_bytes=reserved_attachment_bytes, max_total_bytes=attachment_total_bytes,
                )
            attachments.append(MediaAttachment(index, str(filename), size, is_picture, chapter_samples))
    if require_video and video_streams == 0:
        raise rejected("container does not contain a video stream")
    return MediaProbe(",".join(sorted(approved_video_formats)), tuple(attachments))


def _media_integer(value) -> int:
    """FFprobe uses JSON integers and decimal strings; reject coercible junk."""
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 20:
        return int(value)
    raise ValueError("expected a non-negative integer")


class ScannerService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._health_lock = threading.Lock()
        self._cached_health: ScannerHealth | None = None
        self._cached_health_at = 0.0
        self._clamd_scan_slots = threading.BoundedSemaphore(
            min(max(int(self.settings.clamd_max_inflight_requests), 1), 4)
        )

    def policy_version(self) -> str:
        policy = {
            "implementation": SCANNER_IMPLEMENTATION_POLICY,
            "backend": "clamd-instream",
            "max_file_bytes": self.settings.scanner_max_file_bytes,
            "large_media_enabled": self.settings.large_media_enabled,
            "large_media_max_file_bytes": self.settings.large_media_max_file_bytes,
            "large_media_chunk_bytes": self.settings.large_media_chunk_bytes,
            "large_media_min_chunk_bytes": self.settings.large_media_min_chunk_bytes,
            "large_media_overlap_bytes": self.settings.large_media_overlap_bytes,
            "policy_version": self.settings.scanner_policy_version,
            "media_attachment_max_mib": self.settings.media_attachment_max_mib,
            "media_attachment_total_mib": self.settings.media_attachment_total_mib,
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
            native_deadline = started + max(self.settings.scanner_scan_timeout_seconds, 60)
            if initial_stat.st_size <= self.settings.scanner_max_file_bytes:
                try:
                    infected, threat_name, output = self._scan_descriptor(
                        descriptor,
                        path,
                        expected,
                        heartbeat=heartbeat,
                        should_stop=should_stop,
                        deadline=native_deadline,
                    )
                    scan_method = "clamd_native"
                except ScannerLimitError as native_limit:
                    if not native_limit.can_use_media_fallback:
                        raise
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
                    scan_method = "media_windows_and_attachments"
                else:
                    # Matroska attachments are not necessarily extracted by
                    # ClamAV's native scan. Inspect them even below 2000 MiB.
                    if not infected and os.pread(descriptor, 4, 0) == b"\x1aE\xdf\xa3":
                        deadline = native_deadline
                        probe = self._probe_large_media_descriptor(
                            descriptor, path, deadline=deadline, require_video=False,
                            heartbeat=heartbeat, should_stop=should_stop,
                        )
                        infected, threat_name, attachment_output = self._scan_media_attachments(
                            descriptor, path, expected, probe, deadline=deadline,
                            heartbeat=heartbeat, should_stop=should_stop,
                        )
                        if probe.attachments:
                            scan_method = "clamd_native_with_attachments"
                            output = f"{output}; {attachment_output}"
            else:
                infected, threat_name, output = self._scan_large_media_descriptor(
                    descriptor,
                    path,
                    expected,
                    heartbeat=heartbeat,
                    should_stop=should_stop,
                )
                scan_method = "media_windows_and_attachments"
            self._verify_file_identity(descriptor, path, expected)
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
        minimum_bytes = self.settings.large_media_min_chunk_bytes
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
        if not overlap_bytes < minimum_bytes <= window_bytes:
            raise ScannerPolicyError(
                "TI_LARGE_MEDIA_MIN_CHUNK_MIB must be no larger than the initial window and "
                "must be larger than TI_LARGE_MEDIA_OVERLAP_KIB"
            )
        inflight = self.settings.clamd_max_inflight_requests
        workers = self.settings.per_job_scan_workers
        if not 1 <= inflight <= 4:
            raise ScannerPolicyError(
                "TI_CLAMD_MAX_INFLIGHT_REQUESTS must be between 1 and 4 to match "
                "the bundled ClamD MaxThreads setting"
            )
        if not 1 <= workers <= inflight:
            raise ScannerPolicyError(
                "TI_PER_JOB_SCAN_WORKERS must be positive and no larger than "
                "TI_CLAMD_MAX_INFLIGHT_REQUESTS"
            )
        if not 1 <= self.settings.media_attachment_max_mib <= 64:
            raise ScannerPolicyError("TI_MEDIA_ATTACHMENT_MAX_MIB must be between 1 and 64")
        if not self.settings.media_attachment_max_mib <= self.settings.media_attachment_total_mib <= 256:
            raise ScannerPolicyError("TI_MEDIA_ATTACHMENT_TOTAL_MIB must cover one attachment and be at most 256")
        for binary in (self.settings.ffprobe_binary, self.settings.ffmpeg_binary):
            if not os.path.isabs(binary) or not os.access(binary, os.X_OK):
                raise ScannerPolicyError(f"media inspection tool is not an executable absolute path: {binary}")

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
        deadline: float | None = None,
    ) -> tuple[bool, str | None, str]:
        timeout = max(int(self.settings.scanner_scan_timeout_seconds), 60)
        deadline = min(deadline or float("inf"), time.monotonic() + timeout)
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
        probe = self._probe_large_media_descriptor(
            descriptor, path, deadline=deadline, heartbeat=heartbeat, should_stop=should_stop,
        )
        self._verify_file_identity(descriptor, path, expected)
        infected, threat, attachment_output = self._scan_media_attachments(
            descriptor, path, expected, probe, deadline=deadline,
            heartbeat=heartbeat, should_stop=should_stop,
        )
        if infected:
            return infected, threat, attachment_output

        cancellation = threading.Event()
        shared_heartbeat = self._shared_parallel_heartbeat(heartbeat)
        worker_count = min(max(self.settings.per_job_scan_workers, 1), len(ranges))
        outcomes: dict[int, _WindowScanOutcome] = {}
        infection: _WindowScanOutcome | None = None
        first_error: Exception | None = None

        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="clamd-media-window",
        ) as executor:
            futures = {
                executor.submit(
                    self._scan_large_media_range,
                    descriptor,
                    path,
                    expected,
                    offset=offset,
                    length=length,
                    deadline=deadline,
                    timeout=timeout,
                    heartbeat=shared_heartbeat,
                    should_stop=should_stop,
                    cancellation=cancellation,
                ): index
                for index, (offset, length) in enumerate(ranges, start=1)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    outcome = future.result()
                except _ParallelWindowCancelled:
                    continue
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
                    cancellation.set()
                    continue
                outcomes[index] = outcome
                if outcome.infected and infection is None:
                    infection = outcome
                    cancellation.set()

        self._verify_file_identity(descriptor, path, expected)
        if infection is not None:
            return True, infection.threat_name, "; ".join(infection.replies)[:MAX_REPLY_BYTES]
        if first_error is not None:
            raise first_error
        if len(outcomes) != len(ranges):
            raise ScanInterrupted("large-media scan ended before every byte range completed")

        replies = [reply for index in sorted(outcomes) for reply in outcomes[index].replies]
        return False, None, (
            f"large-media format={probe.format_name} initial_windows={len(ranges)} "
            f"clamd_requests={len(replies)} workers={worker_count} coverage=all-bytes; "
            f"{attachment_output}; "
            + "; ".join(replies)
        )[:MAX_REPLY_BYTES]

    def _scan_large_media_range(
        self,
        descriptor: int,
        path: str,
        expected: FileIdentity,
        *,
        offset: int,
        length: int,
        deadline: float,
        timeout: int,
        heartbeat: Callable[[], bool] | None,
        should_stop: Callable[[], bool] | None,
        cancellation: threading.Event,
    ) -> _WindowScanOutcome:
        if cancellation.is_set():
            raise _ParallelWindowCancelled("large-media sibling window completed terminally")

        def combined_should_stop() -> bool:
            return cancellation.is_set() or bool(should_stop and should_stop())

        try:
            raw_reply = self._scan_descriptor_window(
                descriptor,
                offset=offset,
                length=length,
                deadline=deadline,
                timeout_description=f"large-media {timeout}-second timeout",
                heartbeat=heartbeat,
                should_stop=combined_should_stop,
            )
        except ScanInterrupted as exc:
            if cancellation.is_set() and not (should_stop and should_stop()):
                raise _ParallelWindowCancelled(str(exc)) from exc
            raise

        self._verify_file_identity(descriptor, path, expected)
        output = raw_reply.decode("utf-8", errors="replace")
        reply = f"offset={offset} length={length} {output[:500]}"
        try:
            infected, threat_name = parse_scan_response(output)
        except ScannerLimitError as exc:
            if not exc.can_subdivide:
                raise ScannerPolicyError(
                    f"ClamAV could not inspect a media window ({exc.limit_name}); "
                    "expansion, recursion, and unknown limits cannot be resolved by splitting: "
                    f"{exc}"
                ) from exc
            split = split_large_media_window(
                offset,
                length,
                self.settings.large_media_min_chunk_bytes,
                self.settings.large_media_overlap_bytes,
            )
            if split is None:
                raise ScannerPolicyError(
                    "ClamAV still reached an inspection limit at the configured adaptive "
                    f"minimum window of {self.settings.large_media_min_chunk_mib} MiB: "
                    f"offset={offset} length={length}: {exc}"
                ) from exc

            child_replies = [f"{reply}; subdividing"]
            for child_offset, child_length in split:
                child = self._scan_large_media_range(
                    descriptor,
                    path,
                    expected,
                    offset=child_offset,
                    length=child_length,
                    deadline=deadline,
                    timeout=timeout,
                    heartbeat=heartbeat,
                    should_stop=should_stop,
                    cancellation=cancellation,
                )
                child_replies.extend(child.replies)
                if child.infected:
                    cancellation.set()
                    return _WindowScanOutcome(
                        infected=True,
                        threat_name=child.threat_name,
                        replies=tuple(child_replies),
                    )
            return _WindowScanOutcome(
                infected=False,
                threat_name=None,
                replies=tuple(child_replies),
            )

        if infected:
            cancellation.set()
        return _WindowScanOutcome(
            infected=infected,
            threat_name=threat_name,
            replies=(reply,),
        )

    def _shared_parallel_heartbeat(
        self,
        heartbeat: Callable[[], bool] | None,
    ) -> Callable[[], bool] | None:
        if heartbeat is None:
            return None
        lock = threading.Lock()
        state = {"last": 0.0, "healthy": True}
        minimum_interval = max(min(self.settings.scan_heartbeat_seconds / 2, 5), 1)

        def shared() -> bool:
            with lock:
                now = time.monotonic()
                if now - state["last"] < minimum_interval:
                    return bool(state["healthy"])
                state["healthy"] = bool(heartbeat())
                state["last"] = now
                return bool(state["healthy"])

        return shared

    def _probe_large_media_descriptor(
        self,
        descriptor: int,
        path: str,
        *,
        deadline: float | None = None,
        require_video: bool = True,
        heartbeat: Callable[[], bool] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> MediaProbe:
        command = [
            self.settings.ffprobe_binary,
            "-v",
            "error",
            "-threads", "1",
            "-max_alloc", str(MAX_SINGLE_ALLOCATION_BYTES),
            "-protocol_whitelist",
            "file,pipe",
            "-format_whitelist", ",".join(sorted(LARGE_VIDEO_FORMATS | {LARGE_TRUEHD_FORMAT})),
            "-show_entries",
            "format=format_name:stream=index,codec_type,codec_name,codec_tag_string,extradata_size,nb_frames:"
            "stream_tags=filename,mimetype,handler_name:stream_disposition=attached_pic",
            "-of",
            "json",
            f"/proc/self/fd/{descriptor}",
        ]
        completed = self._run_media_tool(
            command, descriptor, path, deadline=deadline or float("inf"),
            heartbeat=heartbeat, should_stop=should_stop,
        )
        try:
            description = completed.stdout.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise ScannerPolicyError(f"ffprobe returned an invalid media description: {path}") from exc
        return parse_large_media_probe(
            description, path, require_video=require_video,
            attachment_max_bytes=self.settings.media_attachment_max_mib * 1024 * 1024,
            attachment_total_bytes=self.settings.media_attachment_total_mib * 1024 * 1024,
        )

    def _run_media_tool(
        self, command: list[str], descriptor: int, path: str, *, deadline: float,
        heartbeat: Callable[[], bool] | None, should_stop: Callable[[], bool] | None,
        cwd: str | None = None,
        max_stdout_bytes: int = MAX_STDOUT_BYTES,
    ) -> CompletedProcess[bytes]:
        next_heartbeat = 0.0

        def check_active() -> None:
            nonlocal next_heartbeat
            if should_stop and should_stop():
                raise ScanInterrupted("scan interrupted during media inspection")
            if time.monotonic() >= next_heartbeat:
                if heartbeat and not heartbeat():
                    raise ScanInterrupted("scan lease was lost during media inspection")
                next_heartbeat = time.monotonic() + 5

        tool_deadline = min(deadline, time.monotonic() + max(self.settings.large_media_probe_timeout_seconds, 1))
        try:
            completed = run_media_tool(
                command, descriptor=descriptor, deadline=tool_deadline,
                max_file_bytes=0, max_stdout_bytes=max_stdout_bytes,
                check_active=check_active, cwd=cwd,
            )
        except (OSError, MediaToolError) as exc:
            raise ScannerPolicyError(f"bounded media inspection failed: {exc}: {path}") from exc
        if completed.returncode != 0:
            detail = " ".join(completed.stderr.decode("utf-8", "replace").strip().split())[:500]
            raise ScannerPolicyError(
                f"media inspection/extraction failed (exit={completed.returncode})"
                f"{': ' + detail if detail else ''}: {path}"
            )
        return completed

    def _scan_media_attachments(
        self, descriptor: int, path: str, expected: FileIdentity, probe: MediaProbe, *,
        deadline: float, heartbeat: Callable[[], bool] | None,
        should_stop: Callable[[], bool] | None,
    ) -> tuple[bool, str | None, str]:
        self._verify_file_identity(descriptor, path, expected)
        if not probe.attachments:
            return False, None, "attachments=0"
        total_bytes = 0
        for attachment in probe.attachments:
            maximum = min(
                self.settings.media_attachment_max_mib * 1024 * 1024,
                self.settings.media_attachment_total_mib * 1024 * 1024 - total_bytes,
                self.settings.scanner_max_file_bytes,
            )
            if attachment.size_bytes is not None:
                maximum = min(maximum, attachment.size_bytes)
            if maximum <= 0:
                raise _media_policy_error(
                    "media attachments exceed the total extraction budget", path, container=probe.format_name,
                    stream_index=attachment.index, attachment=attachment.filename,
                    extracted_bytes=total_bytes, max_total_bytes=self.settings.media_attachment_total_mib * 1024 * 1024,
                )
            if attachment.chapter_samples is not None:
                content = self._read_mp4_chapter_track(
                    descriptor, path, expected, attachment, maximum=maximum,
                    deadline=deadline, heartbeat=heartbeat, should_stop=should_stop,
                )
            else:
                command = [
                    self.settings.ffmpeg_binary, "-v", "error", "-nostdin", "-n",
                    "-max_alloc", str(MAX_SINGLE_ALLOCATION_BYTES),
                    "-protocol_whitelist", "file,pipe",
                    "-format_whitelist", ",".join(sorted(LARGE_VIDEO_FORMATS | {LARGE_TRUEHD_FORMAT})),
                    "-threads", "1",
                ]
                if not attachment.is_picture:
                    command += [f"-dump_attachment:{attachment.index}", "pipe:1"]
                command += ["-i", f"/proc/self/fd/{descriptor}"]
                if attachment.is_picture:
                    command += [
                        "-map", f"0:{attachment.index}", "-c", "copy", "-frames:v", "1",
                        "-f", "image2pipe", "pipe:1",
                    ]
                else:
                    # Complete header/attachment extraction without decoding a movie.
                    command += ["-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-t", "0", "-f", "null", "-"]
                content = self._run_media_tool(
                    command, descriptor, path, deadline=deadline, heartbeat=heartbeat,
                    should_stop=should_stop, max_stdout_bytes=maximum,
                ).stdout
            self._verify_file_identity(descriptor, path, expected)
            size = len(content)
            if not 0 < size <= maximum or (attachment.size_bytes is not None and size != attachment.size_bytes):
                raise _media_policy_error(
                    "media attachment extraction was incomplete", path, container=probe.format_name,
                    stream_index=attachment.index, attachment=attachment.filename,
                    declared_bytes=attachment.size_bytes, extracted_bytes=size, max_attachment_bytes=maximum,
                )
            total_bytes += size
            # Only one bounded, application-named temporary file exists at a
            # time. Embedded filenames are never passed to filesystem APIs.
            with tempfile.NamedTemporaryFile(prefix="ti-attachment-") as target:
                target.write(content)
                target.flush()
                info = os.fstat(target.fileno())
                # Never subdivide an attachment or send it to media fallback.
                infected, threat, _ = self._scan_descriptor(
                    target.fileno(), target.name, file_identity(info), deadline=deadline,
                    heartbeat=heartbeat, should_stop=should_stop,
                )
                self._verify_file_identity(descriptor, path, expected)
                if infected:
                    return True, threat, f"attachment stream={attachment.index} threat={threat}"
        return False, None, f"attachments={len(probe.attachments)} attachment_bytes={total_bytes}"

    def _read_mp4_chapter_track(
        self, descriptor: int, path: str, expected: FileIdentity, track: MediaAttachment, *,
        maximum: int, deadline: float, heartbeat: Callable[[], bool] | None,
        should_stop: Callable[[], bool] | None,
    ) -> bytes:
        deadline = min(deadline, time.monotonic() + max(self.settings.large_media_probe_timeout_seconds, 1))

        def rejected(reason: str, **details) -> ScannerPolicyError:
            return _media_policy_error(
                f"MP4 chapter track {reason}", path, container="mov/mp4", stream_index=track.index,
                declared_samples=track.chapter_samples, max_bytes=maximum, **details,
            )

        # Normal MOV probing reclassifies chapter text as bin_data and discards
        # its packets. Disable only that reinterpretation, NOT the content scan.
        # Request offsets/sizes, never decoded titles or a large hex payload.
        command = [
            self.settings.ffprobe_binary, "-v", "error", "-threads", "1",
            "-max_alloc", str(MAX_SINGLE_ALLOCATION_BYTES), "-protocol_whitelist", "file,pipe",
            "-format_whitelist", "mov", "-ignore_chapters", "1",
            "-select_streams", str(track.index), "-show_packets", "-show_entries",
            "stream=index,codec_type,codec_name,codec_tag_string,nb_frames:packet=stream_index,pos,size,flags",
            "-of", "json", f"/proc/self/fd/{descriptor}",
        ]
        try:
            completed = self._run_media_tool(
                command, descriptor, path, deadline=deadline, heartbeat=heartbeat, should_stop=should_stop,
            )
        except ScannerPolicyError as exc:
            raise rejected("inspection failed", detail=str(exc)) from exc
        self._verify_file_identity(descriptor, path, expected)
        if completed.stderr.strip():
            raise rejected("reported a demuxing error", detail=completed.stderr.decode("utf-8", "replace"))
        try:
            payload = json.loads(completed.stdout)
            streams, packets = payload["streams"], payload["packets"]
            if not isinstance(streams, list) or len(streams) != 1 or not isinstance(packets, list):
                raise ValueError("invalid stream/packet list")
            stream = streams[0]
            if (_media_integer(stream["index"]) != track.index or stream["codec_type"] != "subtitle"
                    or stream["codec_name"] != "mov_text" or stream["codec_tag_string"] != "text"):
                raise ValueError("not a QuickTime text stream")
            reported_samples = _media_integer(stream["nb_frames"])
            if not 0 < len(packets) == reported_samples == track.chapter_samples <= MAX_CHAPTER_SAMPLES:
                raise rejected(
                    "has an empty, incomplete or inconsistent sample count",
                    observed_samples=len(packets), reported_samples=reported_samples,
                )
        except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
            raise rejected("validation failed", detail=str(exc)) from exc

        content = bytearray()
        previous_end = 0
        next_heartbeat = 0.0
        for number, packet in enumerate(packets):
            if should_stop and should_stop():
                raise ScanInterrupted("scan interrupted during chapter extraction")
            if time.monotonic() >= deadline:
                raise rejected("extraction timed out")
            if time.monotonic() >= next_heartbeat:
                if heartbeat and not heartbeat():
                    raise ScanInterrupted("scan lease was lost during chapter extraction")
                next_heartbeat = time.monotonic() + 5
            try:
                position, size = _media_integer(packet["pos"]), _media_integer(packet["size"])
                if (_media_integer(packet["stream_index"]) != track.index
                        or not isinstance(packet["flags"], str) or "C" in packet["flags"]
                        or size < 2 or position < previous_end or position + size > expected[2]
                        or len(content) + size > maximum):
                    raise ValueError("corrupt, overlapping, out-of-file or oversized packet")
                data = os.pread(descriptor, size, position)
                if len(data) != size or int.from_bytes(data[:2], "big") > size - 2:
                    raise ValueError("truncated packet or invalid text length")
            except (KeyError, TypeError, ValueError) as exc:
                fields = packet if isinstance(packet, dict) else {}
                raise rejected(
                    "packet validation failed", packet=number, detail=str(exc),
                    offset=fields.get("pos"), size=fields.get("size"), file_bytes=expected[2],
                    extracted_bytes=len(content),
                ) from exc
            # Preserve the length prefix, raw text and ALL trailing bytes/boxes.
            # Scanning only exported chapter titles would discard hidden payloads.
            content.extend(data)
            previous_end = position + size
        self._verify_file_identity(descriptor, path, expected)
        return bytes(content)

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

        acquired_slot = False
        try:
            acquired_slot = self._acquire_clamd_scan_slot(
                deadline=deadline,
                check_deadline=check_deadline,
                heartbeat=heartbeat,
                should_stop=should_stop,
            )
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
        finally:
            if acquired_slot:
                self._clamd_scan_slots.release()
        return raw_reply

    def _acquire_clamd_scan_slot(
        self,
        *,
        deadline: float,
        check_deadline: Callable[[], None],
        heartbeat: Callable[[], bool] | None,
        should_stop: Callable[[], bool] | None,
    ) -> bool:
        if self._clamd_scan_slots.acquire(blocking=False):
            return True

        interval = min(max(int(self.settings.scan_heartbeat_seconds), 1), 30)
        while True:
            check_deadline()
            if should_stop and should_stop():
                raise ScanInterrupted("scan interrupted while waiting for ClamD capacity")
            if heartbeat and not heartbeat():
                raise ScanInterrupted("scan lease was lost while waiting for ClamD capacity")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                check_deadline()
            if self._clamd_scan_slots.acquire(timeout=min(interval, remaining)):
                return True

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
