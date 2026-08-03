from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import get_settings

SERVICE = "torrent-intake"
SAFE_EVENT_ID = re.compile(r"^[A-Za-z0-9._-]{1,200}$")


def emit_event(
    event_type: str,
    severity: str,
    message: str,
    *,
    event_id: str | None = None,
    **fields: Any,
) -> Path:
    event_dir = Path(get_settings().event_dir)
    event_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    identifier = event_id or str(uuid.uuid4())
    if not SAFE_EVENT_ID.fullmatch(identifier):
        raise ValueError("event_id contains unsupported characters")
    directory_info = event_dir.lstat()
    if not stat.S_ISDIR(directory_info.st_mode) or stat.S_ISLNK(directory_info.st_mode):
        raise RuntimeError(f"event path is not a real directory: {event_dir}")
    payload: dict[str, Any] = {
        "schema_version": 1,
        "event_id": identifier,
        "event_type": event_type,
        "service": SERVICE,
        "severity": severity,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "message": message[:2000],
    }
    payload.update({key: value for key, value in fields.items() if value is not None})
    descriptor, name = tempfile.mkstemp(prefix=".event-", suffix=".tmp", dir=event_dir)
    temporary = Path(name)
    destination = event_dir / f"{identifier}.json"
    try:
        os.fchmod(descriptor, 0o640)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(event_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return destination
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
