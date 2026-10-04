"""Built-in copy action for verified promoted content; never run through a shell.

Consumers must require .intake-copy-complete.json inside each intake-job-* folder.
An incomplete folder is deliberately never resumed, overwritten, or removed.
Review it and move it aside before explicitly retrying the hook in Intake.
This is not a backup engine or a sandbox against concurrent hostile filesystem
writers. Keep the source and private destination unchanged during the copy.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


COPY_ROOT = Path("/copy-target")
DESTINATION_ROOT = COPY_ROOT / "intake-copies"
MOUNT_MARKER = ".intake-copy-mount"  # Provision manually on the intended export.
COMPLETE_MARKER = ".intake-copy-complete.json"
PENDING_MARKER = ".intake-copy-complete.pending"
RSYNC = "/usr/bin/rsync"


def checked_path(path: Path) -> os.stat_result:
    """Reject traversal and symbolic links in every existing path component."""
    if not path.is_absolute() or ".." in path.parts:
        raise RuntimeError(f"Path must be absolute and traversal-free: {path}")
    current = Path(path.anchor)
    info = current.lstat()
    for part in path.parts[1:]:
        current /= part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError(f"Symbolic links are not allowed: {current}")
    return info


def validate_destination(destination: Path) -> None:
    """Built-in actions can write only below the dedicated operator mount."""
    if (not destination.is_absolute() or ".." in destination.parts
            or not destination.is_relative_to(COPY_ROOT)):
        raise RuntimeError("Built-in copy destination must be /copy-target or a directory below it")
    if not stat.S_ISDIR(checked_path(destination).st_mode):
        raise RuntimeError("Copy destination must already exist as a directory")
    if not os.access(destination, os.R_OK | os.W_OK | os.X_OK):
        raise PermissionError(f"Copy destination is not readable/writable/searchable: {destination}")
    marker = destination / MOUNT_MARKER
    if not stat.S_ISREG(checked_path(marker).st_mode) or not os.access(marker, os.R_OK):
        raise RuntimeError(f"Required destination mount marker must be a readable regular file: {marker}")


def snapshot(root: Path) -> dict[str, list[int | str]]:
    """Record identity without opening symlinks or special files."""
    result: dict[str, list[int | str]] = {}
    pending = [root]
    while pending:
        path = pending.pop()
        info = checked_path(path)
        if stat.S_ISDIR(info.st_mode):
            kind = "directory"
            with os.scandir(path) as entries:
                pending.extend(path / entry.name for entry in entries)
        elif stat.S_ISREG(info.st_mode):
            kind = "file"
        else:
            raise RuntimeError(f"Only regular files and directories may be copied: {path}")
        result[str(path.relative_to(root))] = [
            kind, info.st_size if kind == "file" else 0,
            info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns,
        ]
    return result


def layout(manifest: dict[str, list[int | str]]) -> dict[str, list[int | str]]:
    return {name: info[:2] for name, info in manifest.items()}


def read_completion(path: Path) -> dict:
    info = checked_path(path)
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError("Completion marker is not a regular file")
    with path.open(encoding="utf-8") as stream:
        record = json.load(stream)
    if not isinstance(record, dict):
        raise RuntimeError("Completion marker must contain an object")
    return record


def copy_promoted(source: Path, torrent_hash: str, torrent_name: str, job_id: str, *, destination: Path | None = None) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", job_id):
        raise RuntimeError("Job ID is not a safe folder identifier")
    source_info = checked_path(source)
    if not (stat.S_ISDIR(source_info.st_mode) or stat.S_ISREG(source_info.st_mode)):
        raise RuntimeError("Source must be a regular file or directory")
    destination = DESTINATION_ROOT if destination is None else destination
    destination_info = checked_path(destination)
    if not stat.S_ISDIR(destination_info.st_mode):
        raise RuntimeError("Configured destination must already exist as a directory")
    if source.is_relative_to(destination) or destination.is_relative_to(source):
        raise RuntimeError("Source and destination must not be identical or nested")
    marker = destination / MOUNT_MARKER
    marker_info = checked_path(marker)
    if not stat.S_ISREG(marker_info.st_mode):
        raise RuntimeError(f"Required destination mount marker is not a regular file: {marker}")
    if source.name in {COMPLETE_MARKER, PENDING_MARKER}:
        raise RuntimeError("Source name conflicts with the reserved completion marker")

    before = snapshot(source)
    # Docker bind mounts can give one directory unrelated-looking path names.
    # Detect a destination physically inside the source before mkdir/rsync can
    # copy its own output recursively, even when lexical ancestry differs.
    destination_identity = [destination_info.st_dev, destination_info.st_ino]
    if any(info[0] == "directory" and info[2:4] == destination_identity for info in before.values()):
        raise RuntimeError("Copy destination aliases the source or a directory inside it; choose a separate target")
    request = {
        "source": str(source), "torrent_hash": torrent_hash,
        "torrent_name": torrent_name, "job_id": job_id,
    }
    job_folder = destination / f"intake-job-{job_id}"
    copied = job_folder / source.name
    complete = job_folder / COMPLETE_MARKER
    try:
        job_folder.mkdir(mode=0o700)  # Exclusive reservation; never exist_ok.
    except FileExistsError:
        if not stat.S_ISDIR(checked_path(job_folder).st_mode):
            raise RuntimeError(f"Destination collision: {job_folder}") from None
        try:
            record = read_completion(complete)
        except (OSError, ValueError, RuntimeError) as exc:
            raise RuntimeError(
                f"Incomplete/colliding destination {job_folder}; inspect it and move it "
                "aside before a manual retry. No files were changed."
            ) from exc
        if (record.get("request") != request or record.get("source_snapshot") != before
                or record.get("destination_snapshot") != snapshot(copied)
                or set(job_folder.iterdir()) != {copied, complete}):
            raise RuntimeError(f"Completed destination no longer matches this job: {job_folder}")
        print(f"Already complete; no copy needed: {job_folder}", flush=True)
        return job_folder

    print(f"Copy reservation: {job_folder}; incomplete until {COMPLETE_MARKER} exists", flush=True)
    # Absolute paths and '--' keep filenames out of option/remote-shell parsing.
    # No archive mode: do not preserve owners, groups, devices or symbolic links.
    # Flush each copied file before success can publish a completion marker.
    # This can slow storage-bound copies, but does not reread or rescan payloads.
    # The caller bounds the whole process group with its configured hook timeout.
    subprocess.run([
        RSYNC, "--recursive", "--times", "--fsync", "--ignore-existing", "--no-links",
        "--no-devices", "--no-specials", "--", str(source), str(job_folder) + "/",
    ], check=True)
    after = snapshot(source)
    target = snapshot(copied)
    if before != after or layout(before) != layout(target):
        raise RuntimeError(f"Source changed or copy is incomplete; inspect {job_folder}")
    if set(job_folder.iterdir()) != {copied}:
        raise RuntimeError(f"Unexpected destination entries; inspect {job_folder}")
    # Recheck the operator's mount sentinel before declaring success.
    marker_after = checked_path(marker)
    if (not stat.S_ISREG(marker_after.st_mode)
            or (marker_info.st_dev, marker_info.st_ino, marker_info.st_ctime_ns)
            != (marker_after.st_dev, marker_after.st_ino, marker_after.st_ctime_ns)):
        raise RuntimeError("Destination mount marker changed during copy")
    record = {"request": request, "source_snapshot": before, "destination_snapshot": target}
    # Never publish a truncated JSON file as a completion marker. An exclusive
    # hard link publishes the fully written private file without replacing a name.
    pending_marker = job_folder / PENDING_MARKER
    with pending_marker.open("x", encoding="utf-8") as stream:
        json.dump(record, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.link(pending_marker, complete, follow_symlinks=False)
    pending_marker.unlink()
    print(f"Copy complete: {job_folder}", flush=True)
    return job_folder


def main(*, destination: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    if destination is None:
        parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--torrent-hash", required=True)
    parser.add_argument("--torrent-name", required=True)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        target = destination if destination is not None else args.destination
        if destination is None:
            validate_destination(target)
        copy_promoted(args.source, args.torrent_hash, args.torrent_name, args.job_id, destination=target)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Copy hook failed: {exc}. Source retained; no cleanup was performed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
