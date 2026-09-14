"""Small, private, durable files on the local application data volume."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile


def data_directory() -> Path:
    path = Path(os.environ.get("TI_DATA_DIR", "/app/data"))
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("TI_DATA_DIR must be an absolute local container path without '..'")
    return path


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_private(path: Path, maximum: int = 1024 * 1024) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError(f"Expected a regular application state file: {path.name}")
        value = handle.read(maximum + 1)
        if len(value) > maximum:
            raise ValueError(f"Application state file exceeds its size limit: {path.name}")
        return value


def write_private(path: Path, value: bytes) -> None:
    if path.is_symlink():
        raise ValueError(f"Refusing a symbolic-link application state file: {path.name}")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, value: object) -> None:
    write_private(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode())
