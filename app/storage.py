"""Named NAS choices and small availability gates, not a mount manager."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from sqlalchemy import select

from .config import NasStagingLocation
from .models import Job


class StorageUnavailable(RuntimeError):
    pass


def require_storage(path: str, marker: str | None = None) -> None:
    """Never create a missing staging root or a mount marker automatically."""
    try:
        root = Path(path)
        if not root.is_dir() or not os.access(root, os.R_OK | os.W_OK | os.X_OK):
            raise StorageUnavailable(f"Storage is missing, read-only or inaccessible: {path}")
        if marker:
            expected = Path(marker)
            if expected.is_symlink() or not expected.is_file() or not os.access(expected, os.R_OK):
                raise StorageUnavailable(f"NAS mount marker is missing or unreadable: {marker}")
    except OSError as exc:
        raise StorageUnavailable(f"Cannot access storage {path}: {exc}") from exc


def pin_nas_choice(job: Job, location: NasStagingLocation) -> None:
    job.nas_staging_id = location.id
    job.nas_staging_label = location.label
    job.nas_staging_path = location.path
    job.nas_mount_marker = location.mount_marker


def ensure_nas_choice(job: Job, settings) -> None:
    if job.nas_staging_path:
        return
    # Upgrade old jobs once. NAS jobs keep their actual path; old local jobs
    # retain the legacy fallback, even when a new registry is configured.
    path = ((job.staging_root_actual or job.staging_root_initial)
            if (job.staging_actual or job.staging_preference) == "nas"
            else settings.nas_staging_root)
    match = next((item for item in settings.effective_nas_locations if item.path == path), None)
    pin_nas_choice(job, match or NasStagingLocation(
        id="legacy-" + hashlib.sha256(path.encode()).hexdigest()[:12],
        label="Previous NAS", path=path,
    ))


def pin_existing_jobs(db, settings) -> None:
    for job in db.scalars(select(Job).where(Job.nas_staging_path.is_(None), Job.is_terminal == False)):
        ensure_nas_choice(job, settings)
    db.commit()


def validate_location_changes(db, current, candidate) -> None:
    """Labels/defaults may change; active jobs prevent root/marker removal."""
    if (current.nas_staging_locations == candidate.nas_staging_locations
            and current.nas_staging_root == candidate.nas_staging_root):
        return
    old = {item.id: item for item in current.effective_nas_locations}
    new = {item.id: item for item in candidate.effective_nas_locations}
    for job in db.scalars(select(Job).where(Job.is_terminal == False)):
        ensure_nas_choice(job, current)
        # Grandfathered jobs can refer to a retired location not in the registry.
        # Their saved paths are unchanged by edits to other locations.
        previous = old.get(job.nas_staging_id)
        if previous is None:
            continue
        location = new.get(job.nas_staging_id)
        if location and (location.path, location.mount_marker) == (previous.path, previous.mount_marker):
            continue
        if location is None or (location.path, location.mount_marker) != (job.nas_staging_path, job.nas_mount_marker):
            raise ValueError(f"NAS location used by active job {job.id} cannot be removed or have its path/marker changed")


def require_final_storage(settings, destination: str) -> None:
    """Check the existing media root, not a final subfolder qB may create."""
    target = Path(destination).resolve()
    roots = [Path(value).resolve() for value in settings.allowed_final_parent_prefixes]
    containing = [root for root in roots if root == target or root in target.parents]
    if not containing:
        raise StorageUnavailable("Final destination is outside configured media roots")
    root = max(containing, key=lambda value: len(value.parts))
    require_storage(str(root))
    for location in settings.effective_nas_locations:
        if location.mount_marker:
            marker_root = Path(location.mount_marker).parent.resolve()
            if marker_root == target or marker_root in target.parents:
                require_storage(str(root), location.mount_marker)
