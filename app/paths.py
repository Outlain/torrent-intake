from __future__ import annotations

import os
from pathlib import Path


def _clean_absolute(value: str, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty absolute path")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{label} must not contain control characters")
    path = Path(value.strip())
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{label} could not be resolved safely: {value}") from exc


def path_is_within(candidate: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(candidate), str(root))) == str(root)
    except ValueError:
        return False


def canonical_final_parent(value: str, settings) -> str:
    candidate = _clean_absolute(value, "final_parent")
    allowed_roots = [_clean_absolute(prefix, "final parent prefix") for prefix in settings.allowed_final_parent_prefixes]
    if not any(path_is_within(candidate, root) for root in allowed_roots):
        allowed = ", ".join(str(root) for root in allowed_roots)
        raise ValueError(f"final_parent must be inside one of: {allowed}")

    blocked_values = {
        "/downloads/docker",
        "/app",
        "/state",
        "/var/lib/clamav",
        "/quarantine",
        settings.local_staging_root,
        settings.nas_staging_root,
    }
    for raw_blocked in blocked_values:
        blocked = _clean_absolute(raw_blocked, "blocked operational path")
        if path_is_within(candidate, blocked):
            raise ValueError(f"final_parent points into an operational path: {blocked}")
    return str(candidate)


def canonical_existing_path_within(value: str, root_value: str, label: str) -> Path:
    root = _clean_absolute(root_value, "staging root")
    candidate = _clean_absolute(value, label)
    try:
        candidate = candidate.resolve(strict=True)
        root = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f"{label} does not exist or cannot be resolved: {value}") from exc
    if not path_is_within(candidate, root):
        raise RuntimeError(f"{label} escaped expected staging root {root}: {candidate}")
    return candidate
