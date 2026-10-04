#!/usr/bin/env python3
"""Optional custom-hook wrapper around Intake's built-in copy implementation.

Prefer the built-in copy settings for ordinary copying: no hook file is needed.
Existing deployments may keep mounting this example read-only at /hooks.
"""
from pathlib import Path
import sys

# The installed application lives here; /hooks is intentionally outside it.
sys.path.insert(0, "/app")
from app.copy_action import copy_promoted as _copy_promoted, main

# EDIT THIS only when retaining the advanced custom-script mode.
DESTINATION_ROOT = Path("/copy-target/intake-copies")


def copy_promoted(source: Path, torrent_hash: str, torrent_name: str, job_id: str) -> Path:
    return _copy_promoted(source, torrent_hash, torrent_name, job_id, destination=DESTINATION_ROOT)


if __name__ == "__main__":
    raise SystemExit(main(destination=DESTINATION_ROOT))
