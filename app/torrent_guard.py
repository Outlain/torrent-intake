from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .models import Job
from .paths import canonical_existing_path_within


class TorrentSafetyGuard:
    def tags(self, torrent) -> set[str]:
        return {
            value.strip()
            for value in str(getattr(torrent, "tags", "") or "").split(",")
            if value.strip()
        }

    def is_complete(self, torrent) -> bool:
        progress = float(getattr(torrent, "progress", 0) or 0)
        amount_left = getattr(torrent, "amount_left", None)
        state = str(getattr(torrent, "state", "") or "")
        state_enum = getattr(torrent, "state_enum", None)
        if state_enum is not None and (
            getattr(state_enum, "is_downloading", False)
            or getattr(state_enum, "is_checking", False)
        ):
            return False
        if isinstance(amount_left, int) and amount_left > 0:
            return False
        if progress < 1.0:
            return False
        return state not in {
            "moving",
            "allocating",
            "missingFiles",
            "error",
            "unknown",
            "downloading",
            "stalledDL",
            "forcedDL",
            "metaDL",
            "forcedMetaDL",
            "checkingDL",
            "checkingResumeData",
        }

    @staticmethod
    def is_paused(torrent) -> bool:
        state = str(getattr(torrent, "state", "") or "").lower()
        return "paused" in state or "stopped" in state

    def validate_common(
        self,
        db: Session,
        job: Job,
        torrent,
        *,
        require_paused: bool,
        require_complete: bool = True,
    ) -> str:
        torrent_hash = str(getattr(torrent, "hash", "") or "").lower()
        if not torrent_hash:
            raise RuntimeError("qBittorrent torrent has no usable hash")
        if job.qbt_hash and job.qbt_hash.lower() != torrent_hash:
            raise RuntimeError("job no longer matches the expected qBittorrent torrent")
        expected_tags = {job.managed_tag, job.unique_tag}
        missing = expected_tags - self.tags(torrent)
        if missing:
            raise RuntimeError(f"required Torrent Intake tags are missing: {', '.join(sorted(missing))}")
        other_owners = db.scalar(
            select(func.count())
            .select_from(Job)
            .where(
                Job.id != job.id,
                Job.is_terminal == False,
                or_(func.lower(Job.qbt_hash) == torrent_hash, Job.unique_tag == job.unique_tag),
            )
        ) or 0
        if other_owners:
            raise RuntimeError("another active Torrent Intake job owns this torrent")
        if require_complete and not self.is_complete(torrent):
            raise RuntimeError("torrent is not complete")
        if require_paused and not self.is_paused(torrent):
            raise RuntimeError("torrent is not paused")
        return torrent_hash

    def validate_staging(self, db: Session, job: Job, torrent, *, require_paused: bool) -> Path:
        self.validate_common(db, job, torrent, require_paused=require_paused)
        staging_root = job.staging_root_actual or job.staging_root_initial
        if not staging_root:
            raise RuntimeError("job has no expected staging root")
        save_path = getattr(torrent, "save_path", None)
        content_path = getattr(torrent, "content_path", None)
        if not save_path or not content_path:
            raise RuntimeError("qBittorrent did not report save_path and content_path")
        canonical_existing_path_within(str(save_path), staging_root, "torrent save_path")
        return canonical_existing_path_within(str(content_path), staging_root, "torrent content_path")

    def validate_destination(self, db: Session, job: Job, torrent) -> Path:
        self.validate_common(db, job, torrent, require_paused=True)
        save_path = Path(str(getattr(torrent, "save_path", "") or "")).resolve(strict=False)
        expected = Path(job.final_parent).resolve(strict=False)
        if save_path != expected:
            raise RuntimeError(f"torrent is not at its canonical final destination: {save_path}")
        content_path = getattr(torrent, "content_path", None)
        if not content_path:
            raise RuntimeError("qBittorrent did not report promoted content_path")
        return canonical_existing_path_within(
            str(content_path),
            str(expected),
            "promoted torrent content_path",
        )

    def validate_quarantine_destination(
        self,
        db: Session,
        job: Job,
        torrent,
        destination: Path,
    ) -> Path:
        self.validate_common(db, job, torrent, require_paused=True)
        save_path = Path(str(getattr(torrent, "save_path", "") or "")).resolve(strict=False)
        if save_path != destination:
            raise RuntimeError(f"torrent is not at its expected quarantine destination: {save_path}")
        content_path = getattr(torrent, "content_path", None)
        if not content_path:
            raise RuntimeError("qBittorrent did not report quarantined content_path")
        return canonical_existing_path_within(
            str(content_path),
            str(destination),
            "quarantined torrent content_path",
        )
