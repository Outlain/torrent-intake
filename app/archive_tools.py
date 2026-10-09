"""Bounded archive inspection. Extracted copies never use torrent/member paths."""
from __future__ import annotations

import ctypes
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import resource
import selectors
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from collections.abc import Callable
from dataclasses import asdict, dataclass


BLOCK_BYTES = 1024 * 1024
MAX_MESSAGE_BYTES = 64 * 1024
MEMBER_FILENAME = re.compile(r"member-[0-9]{6,12}(?:\.[a-z0-9]{1,10})?\Z")


class ArchiveError(RuntimeError):
    pass


@dataclass(frozen=True)
class ArchiveLimits:
    expanded_bytes: int
    files: int
    depth: int
    reserve_bytes: int
    timeout_seconds: int


def archive_format(descriptor: int) -> str | None:
    magic = os.pread(descriptor, 8, 0)
    if magic.startswith(b"Rar!\x1a\x07\x00"):
        return "rar"
    if magic == b"Rar!\x1a\x07\x01\x00":
        return "rar5"
    if magic[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "zip"
    return None


def require_archive_storage(scratch_dir: str, mount_marker: str | None = None) -> None:
    """Custom storage is operator-mounted; never create it or fall back locally."""
    try:
        base = Path(scratch_dir)
        if (not base.is_absolute() or not base.is_dir() or base.resolve() != base
                or not os.access(base, os.R_OK | os.W_OK | os.X_OK)):
            raise ArchiveError(f"archive scratch storage is missing, inaccessible or symlinked: {base}")
        if mount_marker:
            marker = Path(mount_marker)
            if (not marker.is_relative_to(base) or marker.resolve() != marker or not marker.is_file()
                    or not os.access(marker, os.R_OK)):
                raise ArchiveError(f"archive scratch mount marker is missing or unreadable: {marker}")
    except OSError as exc:
        raise ArchiveError(f"archive scratch storage is unavailable: {exc}") from exc


def _workspace_root(data_dir: str, scratch_dir: str | None = None, mount_marker: str | None = None) -> Path:
    try:
        if scratch_dir:
            require_archive_storage(scratch_dir, mount_marker)
        root = Path(scratch_dir or data_dir) / "archive-scan"
        root.mkdir(mode=0o700, parents=not bool(scratch_dir), exist_ok=True)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ArchiveError("archive scratch workspace must be a private, owned directory")
        return root
    except OSError as exc:
        raise ArchiveError(f"cannot prepare archive scratch workspace: {exc}") from exc


@contextmanager
def _workspace_lock(root: Path, *, cleanup: bool = False):
    """A shared mount may serve several controllers; cleanup must see no scans."""
    descriptor = os.open(root / "workspace.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ArchiveError("archive workspace lock must be a private, owned regular file")
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_EX if cleanup else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            if not cleanup:
                raise ArchiveError("archive scratch cleanup is in progress; retry the scan")
            yield False
        else:
            yield True
    finally:
        os.close(descriptor)


def cleanup_archive_workspaces(data_dir: str, scratch_dir: str | None = None, mount_marker: str | None = None) -> None:
    """Called only at startup while the exclusive controller lock is held."""
    root = _workspace_root(data_dir, scratch_dir, mount_marker)
    with _workspace_lock(root, cleanup=True) as acquired:
        if acquired:
            for child in root.iterdir():
                if re.fullmatch(r"scan-[a-z0-9_]{8}", child.name) and child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)


def scan_archive(
    descriptor: int, *, data_dir: str, limits: ArchiveLimits,
    check_active: Callable[[], None],
    scan_member: Callable[[str, str], tuple[bool, str | None, str]],
    scratch_dir: str | None = None, mount_marker: str | None = None,
) -> tuple[bool, str | None, str]:
    """One helper extracts; the parent scans each member before acknowledging it.

    Only one leaf member is on disk at a time. Nested archive copies share the
    same extraction budget. EOF, helper failure, and incomplete output fail closed.
    """
    deadline = time.monotonic() + limits.timeout_seconds
    check_active()
    root = _workspace_root(data_dir, scratch_dir, mount_marker)
    with _workspace_lock(root), tempfile.TemporaryDirectory(prefix="scan-", dir=root) as temporary:
        command = [sys.executable, os.path.abspath(__file__), str(descriptor), json.dumps(asdict(limits))]
        with subprocess.Popen(
            command, pass_fds=(descriptor,), cwd=temporary, start_new_session=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
        ) as process:
            buffer = bytearray()
            errors = bytearray()
            completed = None
            scanned = 0
            methods: dict[str, int] = {}
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
                    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                    while selector.get_map() or process.poll() is None:
                        check_active()
                        if time.monotonic() >= deadline:
                            raise ArchiveError("archive inspection timed out")
                        for key, _ in selector.select(timeout=0.2):
                            chunk = os.read(key.fileobj.fileno(), MAX_MESSAGE_BYTES)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            target = buffer if key.data == "stdout" else errors
                            target.extend(chunk)
                            if len(target) > MAX_MESSAGE_BYTES:
                                raise ArchiveError("archive helper exceeded its output limit")
                            if key.data != "stdout":
                                continue
                            while b"\n" in buffer:
                                line, _, rest = buffer.partition(b"\n")
                                buffer[:] = rest
                                message = json.loads(line)
                                if not isinstance(message, dict) or completed is not None:
                                    raise ArchiveError("invalid archive helper response")
                                if message.get("type") == "done":
                                    completed = message
                                    continue
                                if message.get("type") != "member":
                                    raise ArchiveError("unexpected archive helper response")
                                name, filename, size = message.get("name"), message.get("file"), message.get("size")
                                if (not isinstance(filename, str) or not MEMBER_FILENAME.fullmatch(filename)
                                        or not isinstance(name, str) or len(name) > 20000
                                        or type(size) is not int or not 0 <= size <= limits.expanded_bytes):
                                    raise ArchiveError("invalid extracted-member description")
                                member_path = Path(temporary) / filename
                                info = member_path.lstat()
                                if not stat.S_ISREG(info.st_mode) or info.st_size != size or info.st_nlink != 1:
                                    raise ArchiveError("extracted member identity/size is invalid")
                                scanned += 1
                                if scanned > limits.files:
                                    raise ArchiveError("archive member count exceeds its limit")
                                infected, threat, method = scan_member(str(member_path), name)
                                methods[method] = methods.get(method, 0) + 1
                                if infected:
                                    return True, threat, f"archive member={json.dumps(name)}: {threat}"
                                check_active()
                                process.stdin.write(b"ok\n")
                    returncode = process.wait()
                if returncode != 0:
                    detail = " ".join(errors.decode("utf-8", "replace").split())[:1000]
                    raise ArchiveError(f"archive extraction failed (exit={returncode}): {detail}")
                if buffer or not completed or completed.get("scanned") != scanned:
                    raise ArchiveError("archive extraction ended without a complete member manifest")
                return False, None, (
                    f"archive members={scanned} entries={completed['entries']} "
                    f"expanded_bytes={completed['expanded']} member_methods={json.dumps(methods, sort_keys=True)}"
                )
            except (OSError, ValueError, KeyError) as exc:
                raise ArchiveError(f"archive inspection failed: {exc}") from exc
            finally:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait()


class _Reader:
    """Minimal libarchive interface, loaded only in the restricted helper."""
    def __init__(self):
        self.lib = ctypes.CDLL("libarchive.so.13")
        signatures = {
            "read_new": (ctypes.c_void_p, []),
            "read_free": (ctypes.c_int, [ctypes.c_void_p]),
            "read_support_filter_none": (ctypes.c_int, [ctypes.c_void_p]),
            "read_open_fd": (ctypes.c_int, [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t]),
            "read_next_header": (ctypes.c_int, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]),
            "read_data": (ctypes.c_ssize_t, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]),
            "error_string": (ctypes.c_char_p, [ctypes.c_void_p]),
            "entry_pathname_utf8": (ctypes.c_char_p, [ctypes.c_void_p]),
            "entry_size": (ctypes.c_int64, [ctypes.c_void_p]),
            "entry_size_is_set": (ctypes.c_int, [ctypes.c_void_p]),
            "entry_filetype": (ctypes.c_uint, [ctypes.c_void_p]),
            "entry_hardlink": (ctypes.c_char_p, [ctypes.c_void_p]),
            "entry_symlink": (ctypes.c_char_p, [ctypes.c_void_p]),
            "entry_is_encrypted": (ctypes.c_int, [ctypes.c_void_p]),
        }
        for kind in ("rar", "rar5", "zip"):
            signatures[f"read_support_format_{kind}"] = (ctypes.c_int, [ctypes.c_void_p])
        for name, (result, arguments) in signatures.items():
            function = getattr(self.lib, "archive_" + name)
            function.restype, function.argtypes = result, arguments
            setattr(self, name, function)

    def check(self, result: int, archive) -> int:
        if result < 0:  # Warnings also mean inspection was incomplete.
            message = self.error_string(archive)
            raise ArchiveError(message.decode("utf-8", "replace") if message else "archive parser failed")
        return result


def _vint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 63, 7):
        if offset >= len(data):
            break
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if not byte & 128:
            return value, offset
    raise ArchiveError("invalid or truncated RAR header")


def _validate_rar(descriptor: int, kind: str, max_headers: int) -> list[tuple[int, int | None]]:
    """Require complete single-volume framing, including the end marker.

    Libarchive can report EOF at a missing end header or ignore trailing bytes.
    Walk bounded headers using pread; compressed payloads are still decoded and
    checked by libarchive. No compressed bytes are loaded during this pass.
    """
    size = os.fstat(descriptor).st_size
    position = 7 if kind == "rar" else 8
    members = []
    for number in range(max_headers):
        prefix = os.pread(descriptor, 16, position)
        if len(prefix) < 7:
            raise ArchiveError("RAR end header is missing or truncated")
        data_size = 0
        if kind == "rar":
            crc, header_type, flags, header_size = struct.unpack_from("<HBHH", prefix)
            if header_size < 7:
                raise ArchiveError("invalid RAR header size")
            header = os.pread(descriptor, header_size, position)
            if len(header) != header_size or zlib.crc32(header[2:]) & 0xffff != crc:
                raise ArchiveError("RAR header is truncated or has an invalid checksum")
            if number == 0 and (header_type != 0x73 or header_size < 13):
                raise ArchiveError("invalid RAR main header")
            if header_type == 0x73 and flags & 0x80:
                raise ArchiveError("encrypted RAR headers cannot be inspected")
            if (header_type == 0x73 and flags & 1) or (header_type == 0x74 and flags & 3):
                raise ArchiveError("multipart RAR archives require all volumes and are not supported")
            if header_type == 0x74 and flags & 4:
                raise ArchiveError("encrypted RAR member cannot be inspected")
            if flags & 0x8000:
                if header_size < 11:
                    raise ArchiveError("truncated RAR data-size field")
                data_size = int.from_bytes(header[7:11], "little")
            if header_type in (0x74, 0x7a) and flags & 0x100:
                if header_size < 40:
                    raise ArchiveError("truncated RAR large-file header")
                data_size |= int.from_bytes(header[32:36], "little") << 32
            if header_type == 0x74:
                if header_size < 32:
                    raise ArchiveError("truncated RAR file header")
                unpacked_size = int.from_bytes(header[11:15], "little")
                if flags & 0x100:
                    unpacked_size |= int.from_bytes(header[36:40], "little") << 32
                members.append((unpacked_size, int.from_bytes(header[16:20], "little")))
            end = header_type == 0x7b
            if end and flags & 1:
                raise ArchiveError("multipart RAR archives are not supported")
            if header_type not in range(0x73, 0x7c):
                raise ArchiveError("unsupported RAR block type")
        else:
            header_data_size, offset = _vint(prefix, 4)
            header_size = offset + header_data_size
            if offset > 7 or header_data_size > 2 * 1024**2:
                raise ArchiveError("invalid RAR5 header size")
            header = os.pread(descriptor, header_size, position)
            if len(header) != header_size or zlib.crc32(header[4:]) != int.from_bytes(header[:4], "little"):
                raise ArchiveError("RAR header is truncated or has an invalid checksum")
            header_type, offset = _vint(header, offset)
            if header_type == 4:
                raise ArchiveError("encrypted RAR headers cannot be inspected")
            if number == 0 and header_type != 1:
                raise ArchiveError("invalid RAR main header")
            flags, offset = _vint(header, offset)
            if flags & 0x18:
                raise ArchiveError("multipart RAR archives require all volumes and are not supported")
            extra_size = 0
            if flags & 1:
                extra_size, offset = _vint(header, offset)
            if flags & 2:
                data_size, offset = _vint(header, offset)
            if extra_size > len(header) - offset:
                raise ArchiveError("invalid RAR extra-area size")
            if header_type == 2 and extra_size:
                extra_offset = len(header) - extra_size
                while extra_offset < len(header):
                    record_size, record_start = _vint(header, extra_offset)
                    record_end = record_start + record_size
                    if not record_size or record_end > len(header):
                        raise ArchiveError("invalid RAR extra record")
                    record_type, _ = _vint(header[:record_end], record_start)
                    if record_type == 1:
                        raise ArchiveError("encrypted RAR member cannot be inspected")
                    extra_offset = record_end
            if header_type == 1:
                main_flags, _ = _vint(header, offset)
                if main_flags & 3:
                    raise ArchiveError("multipart RAR archives require all volumes and are not supported")
            if header_type == 2:
                file_flags, offset = _vint(header, offset)
                unpacked_size, offset = _vint(header, offset)
                _, offset = _vint(header, offset)  # Attributes.
                if file_flags & 8:
                    raise ArchiveError("RAR member has no declared size")
                if file_flags & 2:
                    offset += 4  # Optional mtime.
                checksum = None
                if file_flags & 4:
                    if offset + 4 > len(header):
                        raise ArchiveError("truncated RAR checksum field")
                    checksum = int.from_bytes(header[offset:offset + 4], "little")
                elif not file_flags & 1:  # Directories have no payload checksum.
                    raise ArchiveError("RAR5 member requires a CRC32 checksum for complete inspection")
                members.append((unpacked_size, checksum))
            end = header_type == 5
            if end:
                end_flags, _ = _vint(header, offset)
                if end_flags & 1:
                    raise ArchiveError("multipart RAR archives are not supported")
            if header_type not in (1, 2, 3, 5):
                raise ArchiveError("unsupported RAR block type")
        position += header_size + data_size
        if position > size:
            raise ArchiveError("RAR member data is truncated")
        if end:
            if position != size:
                raise ArchiveError("RAR has trailing data after its end header")
            return members
    raise ArchiveError("RAR header count exceeds its limit")


def _validate_container(descriptor: int, kind: str, max_headers: int) -> list[tuple[int, int | None]] | None:
    if kind in ("rar", "rar5"):
        return _validate_rar(descriptor, kind, max_headers)
    elif kind == "zip":
        header = os.pread(descriptor, 4, 0)
        # A sequential reader can accept truncated ZIPs without a central directory.
        # Require a complete end record and a single disk before using libarchive.
        size = os.fstat(descriptor).st_size
        tail = os.pread(descriptor, min(size, 65557), max(0, size - 65557))
        offset = tail.rfind(b"PK\x05\x06")
        if offset < 0 or len(tail) - offset < 22:
            raise ArchiveError("ZIP central directory is missing or truncated")
        _, disk, directory_disk, disk_entries, total_entries, _, _, comment_size = struct.unpack_from("<4s4H2LH", tail, offset)
        if disk or directory_disk or disk_entries != total_entries or header.startswith(b"PK\x07\x08"):
            raise ArchiveError("multipart ZIP archives are not supported")
        if offset + 22 + comment_size != len(tail):
            raise ArchiveError("ZIP end record is incomplete or has trailing data")
        end_offset = size - len(tail) + offset
        locator = os.pread(descriptor, 20, max(0, end_offset - 20))
        if locator.startswith(b"PK\x06\x07"):
            _, zip64_disk, _, disk_count = struct.unpack("<4sLQL", locator)
            if zip64_disk or disk_count != 1:
                raise ArchiveError("multipart ZIP64 archives are not supported")


def _member_name(raw: bytes | None) -> str:
    if raw is None:
        raise ArchiveError("archive member has no valid UTF-8 filename")
    name = raw.decode("utf-8", "strict")
    normalized = name.replace("\\", "/")
    if (not name or len(name) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or PurePosixPath(normalized).is_absolute() or PureWindowsPath(name).drive
            or ".." in normalized.split("/")):
        raise ArchiveError(f"unsafe archive member path: {name[:200]!r}")
    return normalized


class _Extractor:
    def __init__(self, limits: ArchiveLimits):
        self.limits = limits
        self.reader = _Reader()
        self.entries = 0
        self.expanded = 0
        self.scanned = 0

    def extract(self, descriptor: int, depth: int = 1, parents: tuple[str, ...] = ()) -> None:
        if depth > self.limits.depth:
            raise ArchiveError("archive nesting depth exceeds its limit")
        kind = archive_format(descriptor)
        if kind is None:
            raise ArchiveError("unsupported archive format")
        manifest = _validate_container(descriptor, kind, self.limits.files * 4 + 1024)
        member_index = 0
        reader = self.reader
        archive = reader.read_new()
        if not archive:
            raise ArchiveError("cannot allocate archive reader")
        try:
            reader.check(reader.read_support_filter_none(archive), archive)
            reader.check(getattr(reader, "read_support_format_" + kind)(archive), archive)
            os.lseek(descriptor, 0, os.SEEK_SET)
            reader.check(reader.read_open_fd(archive, descriptor, BLOCK_BYTES), archive)
            entry = ctypes.c_void_p()
            buffer = ctypes.create_string_buffer(BLOCK_BYTES)
            while True:
                result = reader.check(reader.read_next_header(archive, ctypes.byref(entry)), archive)
                if result == 1:  # ARCHIVE_EOF
                    if manifest is not None and member_index != len(manifest):
                        raise ArchiveError("archive parser did not inspect every declared member")
                    break
                self.entries += 1
                if self.entries > self.limits.files:
                    raise ArchiveError("archive entry count exceeds its limit")
                name = _member_name(reader.entry_pathname_utf8(entry))
                label = "!/".join((*parents, name))
                if reader.entry_is_encrypted(entry):
                    raise ArchiveError(f"encrypted archive member: {label!r}")
                kind_bits = reader.entry_filetype(entry)
                if (reader.entry_hardlink(entry) is not None or reader.entry_symlink(entry) is not None
                        or kind_bits not in (stat.S_IFREG, stat.S_IFDIR)):
                    raise ArchiveError(f"archive links or special files are not supported: {label!r}")
                size = reader.entry_size(entry)
                if not reader.entry_size_is_set(entry) or size < 0:
                    raise ArchiveError(f"archive member has no valid declared size: {label!r}")
                checksum = None
                if manifest is not None:
                    if member_index >= len(manifest) or size != manifest[member_index][0]:
                        raise ArchiveError("archive parser disagrees with the declared member manifest")
                    checksum = manifest[member_index][1]
                    member_index += 1
                if size > self.limits.expanded_bytes - self.expanded:
                    raise ArchiveError("archive expanded-byte limit exceeded")
                if kind_bits == stat.S_IFDIR:
                    if size or reader.check(reader.read_data(archive, buffer, BLOCK_BYTES), archive):
                        raise ArchiveError("archive directory contains unexpected data")
                    continue
                if shutil.disk_usage(".").free < size + self.limits.reserve_bytes:
                    raise ArchiveError("insufficient archive scratch space (including free-space reserve)")
                suffix = PurePosixPath(name).suffix.lower()
                if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
                    suffix = ""
                filename = f"member-{self.entries:06d}{suffix}"
                written = 0
                actual_checksum = 0
                with open(filename, "xb", buffering=0) as target:
                    os.chmod(filename, 0o600)
                    while True:
                        count = reader.check(reader.read_data(archive, buffer, BLOCK_BYTES), archive)
                        if count == 0:
                            break
                        if (count > size - written or count > self.limits.expanded_bytes - self.expanded):
                            raise ArchiveError("archive member exceeds its declared size or expanded-byte limit")
                        if shutil.disk_usage(".").free < count + self.limits.reserve_bytes:
                            raise ArchiveError("archive scratch free-space reserve reached")
                        content = buffer.raw[:count]
                        if target.write(content) != count:
                            raise ArchiveError("archive scratch write was incomplete")
                        actual_checksum = zlib.crc32(content, actual_checksum)
                        written += count
                        self.expanded += count
                if written != size:
                    raise ArchiveError(f"archive member is truncated: {label!r}")
                if checksum is not None and actual_checksum != checksum:
                    raise ArchiveError(f"archive member checksum mismatch: {label!r}")
                os.chmod(filename, 0o400)
                with open(filename, "rb") as member:
                    if archive_format(member.fileno()):
                        self.extract(member.fileno(), depth + 1, (*parents, name))
                    else:
                        print(json.dumps({"type": "member", "file": filename, "name": label, "size": size}), flush=True)
                        if sys.stdin.buffer.readline(16) != b"ok\n":
                            raise ArchiveError("archive scan was interrupted before member acknowledgement")
                        self.scanned += 1
                os.unlink(filename)
        finally:
            reader.read_free(archive)


def main() -> None:
    try:
        descriptor = int(sys.argv[1])
        limits = ArchiveLimits(**json.loads(sys.argv[2]))
        os.umask(0o077)
        resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
        resource.setrlimit(resource.RLIMIT_FSIZE, (limits.expanded_bytes, limits.expanded_bytes))
        cpu = max(1, math.ceil(limits.timeout_seconds))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        extractor = _Extractor(limits)
        extractor.extract(descriptor)
        print(json.dumps({"type": "done", "entries": extractor.entries,
                          "expanded": extractor.expanded, "scanned": extractor.scanned}), flush=True)
    except Exception as exc:
        print(str(exc)[:4000], file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
