from __future__ import annotations
from datetime import datetime, timedelta
import logging
import os
from pathlib import Path
import shutil
import threading
import time
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from .config import get_settings
from .models import Job
from .qbt import QbtService, TorrentAlreadyExistsError
from .scanner import ScannerService
from .telegram import TelegramService

_ACTIVE_LONG_RUNNING_JOBS: set[str] = set()
_ACTIVE_LONG_RUNNING_JOBS_LOCK = threading.Lock()


class JobService:
    TERMINAL_STATES = {"done", "infected_deleted", "error"}
    QBT_ERROR_STATES = {"error", "missingFiles"}
    QBT_ATTENTION_STATES = {
        "allocating",
        "checkingDL",
        "checkingResumeData",
        "error",
        "forcedMetaDL",
        "metaDL",
        "missingFiles",
        "pausedDL",
        "pausedUP",
        "queuedDL",
        "stalledDL",
        "stalledUP",
        "unknown",
    }

    def __init__(self) -> None:
        self.settings = get_settings()
        self.qbt = QbtService()
        self.scanner = ScannerService()
        self.telegram = TelegramService()
        self.logger = logging.getLogger(__name__)

    def submit_job(self, db: Session, *, magnet_uri: str, final_parent: str, final_category: str | None,
                   staging_preference: str) -> Job:
        existing_torrent = self.qbt.find_existing_from_magnet(magnet_uri)
        if existing_torrent is not None:
            raise ValueError(
                self._duplicate_torrent_message(
                    db,
                    torrent_hash=getattr(existing_torrent, "hash", None),
                    torrent_name=getattr(existing_torrent, "name", None),
                    exclude_job_id=None,
                )
            )

        staging_root = self._root_for_preference(staging_preference)
        job = self._create_job_record(
            db,
            magnet_uri=magnet_uri,
            final_parent=final_parent,
            final_category=final_category,
            staging_preference=staging_preference,
            staging_root=staging_root,
        )

        try:
            self.qbt.add_torrent(
                magnet_uri=magnet_uri,
                save_path=staging_root,
                tags=[self.settings.managed_tag, job.unique_tag],
                category=self.settings.intake_category,
            )
            self._resolve_hash_for_job(db, job)
            self._evaluate_staging_now(db, job)
        except TorrentAlreadyExistsError as exc:
            error_text = self._duplicate_torrent_message(
                db,
                torrent_hash=exc.torrent_hash,
                torrent_name=exc.torrent_name,
                exclude_job_id=job.id,
            )
            self.logger.warning("Rejected duplicate intake submit for job %s: %s", job.id, error_text)
            db.delete(job)
            db.commit()
            raise ValueError(error_text) from exc
        except Exception as exc:
            error_text = str(exc).strip() or repr(exc)
            self.logger.exception("Failed to submit job %s to qBittorrent", job.id)
            self._mark(job, "error", error=f"qBittorrent submission failed: {error_text}")
            db.add(job)
            db.commit()
            raise RuntimeError(f"Failed to submit to qBittorrent: {error_text}") from exc
        return job

    def _create_job_record(
        self,
        db: Session,
        *,
        magnet_uri: str,
        final_parent: str,
        final_category: str | None,
        staging_preference: str,
        staging_root: str,
    ) -> Job:
        last_exc: Exception | None = None
        for _ in range(5):
            job = Job(
                id=str(uuid4()),
                magnet_uri=magnet_uri,
                final_parent=final_parent,
                final_category=final_category,
                staging_preference=staging_preference,
                staging_actual=staging_preference,
                staging_root_initial=staging_root,
                staging_root_actual=staging_root,
                managed_tag=self.settings.managed_tag,
                unique_tag=self._generate_unique_tag(db),
                state="adding_to_qbt",
                updated_at=datetime.utcnow(),
            )
            db.add(job)
            try:
                db.commit()
                db.refresh(job)
                return job
            except IntegrityError as exc:
                db.rollback()
                last_exc = exc
                self.logger.warning("Retrying job record creation after unique constraint collision")
        raise RuntimeError("Failed to allocate a unique intake tag after multiple attempts") from last_exc

    def _generate_unique_tag(self, db: Session) -> str:
        reserved_tags = self._reserved_unique_tags(db)
        for _ in range(20):
            candidate = f"ti_job_{uuid4().hex[:12]}"
            if candidate not in reserved_tags:
                return candidate
        raise RuntimeError("Failed to generate a unique intake tag after multiple attempts")

    def _reserved_unique_tags(self, db: Session) -> set[str]:
        db_tags = {
            tag for tag in db.scalars(select(Job.unique_tag))
            if isinstance(tag, str) and tag.startswith("ti_job_")
        }
        qbt_tags: set[str] = set()
        for torrent in self.qbt.list_torrents():
            torrent_tags = getattr(torrent, "tags", "") or ""
            for tag in torrent_tags.split(","):
                normalized = tag.strip()
                if normalized.startswith("ti_job_"):
                    qbt_tags.add(normalized)
        return db_tags | qbt_tags

    def retry_job(self, db: Session, *, job_id: str) -> Job:
        job = db.get(Job, job_id)
        if not job:
            raise LookupError("Job not found")
        if job.state != "error":
            raise ValueError("Only jobs in error state can be retried")

        staging_root = job.staging_root_actual or job.staging_root_initial or self._root_for_preference(job.staging_preference)
        self._prepare_job_for_retry(job)

        try:
            torrent = self._find_live_torrent_for_job(job)
            if torrent is None and job.qbt_hash:
                job.qbt_hash = None

            if not job.qbt_hash:
                self.qbt.add_torrent(
                    magnet_uri=job.magnet_uri,
                    save_path=staging_root,
                    tags=[self.settings.managed_tag, job.unique_tag],
                    category=self.settings.intake_category,
                )
                self._resolve_hash_for_job(db, job)
                self._evaluate_staging_now(db, job)
                return job

            self._ensure_job_can_track_torrent(db, job, torrent)
            self._sync_job_from_torrent(job, torrent)
            self._raise_for_qbt_error_state(torrent)
            self._apply_local_staging_policy(db, job, torrent)
            if job.state != "waiting_for_local_space":
                job.state = self._state_for_retry(job, torrent)
            job.is_terminal = False
            db.add(job)
            db.commit()
            db.refresh(job)
            return job
        except TorrentAlreadyExistsError as exc:
            message = self._duplicate_torrent_message(
                db,
                torrent_hash=exc.torrent_hash,
                torrent_name=exc.torrent_name,
                exclude_job_id=job.id,
            )
            existing_torrent = self.qbt.get_torrent(exc.torrent_hash) if exc.torrent_hash else None
            tracked_job = self._find_job_by_hash(db, torrent_hash=exc.torrent_hash, exclude_job_id=job.id)

            if existing_torrent is not None and tracked_job is None:
                job.qbt_hash = getattr(existing_torrent, "hash", None) or exc.torrent_hash
                self._sync_job_from_torrent(job, existing_torrent)
                self._raise_for_qbt_error_state(existing_torrent)
                self._apply_local_staging_policy(db, job, existing_torrent)
                if job.state != "waiting_for_local_space":
                    job.state = self._state_for_retry(job, existing_torrent)
                job.is_terminal = False
                db.add(job)
                db.commit()
                db.refresh(job)
                self.logger.info(
                    "Attached retry job %s to existing qBittorrent torrent %s",
                    job.id,
                    job.qbt_hash,
                )
                return job

            self.logger.warning("Retry rejected for stale duplicate job %s: %s", job.id, message)
            self._mark(job, "error", error=message)
            db.add(job)
            db.commit()
            db.refresh(job)
            raise ValueError(message) from exc
        except Exception as exc:
            error_text = str(exc).strip() or repr(exc)
            self.logger.exception("Failed to retry job %s", job.id)
            self._mark(job, "error", error=f"retry failed: {error_text}")
            db.add(job)
            db.commit()
            raise RuntimeError(f"Failed to retry job: {error_text}") from exc

    def delete_job(self, db: Session, *, job_id: str) -> None:
        job = db.get(Job, job_id)
        if not job:
            raise LookupError("Job not found")
        # Intake-only removal. Never delete, pause, or modify the qBittorrent torrent here.
        db.delete(job)
        db.commit()

    def retry_jobs(self, db: Session, *, job_ids: list[str]) -> dict[str, object]:
        return self._bulk_apply(job_ids, lambda selected_id: self.retry_job(db, job_id=selected_id))

    def delete_jobs(self, db: Session, *, job_ids: list[str]) -> dict[str, object]:
        return self._bulk_apply(job_ids, lambda selected_id: self.delete_job(db, job_id=selected_id))

    def move_waiting_jobs_to_nas(self, db: Session, *, job_ids: list[str]) -> dict[str, object]:
        return self._bulk_apply(job_ids, lambda selected_id: self.move_waiting_job_to_nas(db, job_id=selected_id))

    def delete_jobs_by_states(self, db: Session, *, states: set[str]) -> dict[str, object]:
        if not states:
            return self._empty_bulk_result()
        jobs = list(db.scalars(select(Job).where(Job.state.in_(tuple(states))).order_by(Job.created_at.desc())))
        return self.delete_jobs(db, job_ids=[job.id for job in jobs])

    def move_waiting_job_to_nas(self, db: Session, *, job_id: str) -> Job:
        job = db.get(Job, job_id)
        if not job:
            raise LookupError("Job not found")
        if job.state != "waiting_for_local_space":
            raise ValueError("Only jobs waiting for local space can be moved to NAS")
        if job.staging_preference != "local" or job.staging_actual != "local":
            raise ValueError("Only queued local-staging jobs can be moved to NAS")
        if not job.qbt_hash:
            raise ValueError("Queued job is not linked to a qBittorrent hash yet")

        free_bytes, safe_free_bytes, reserved_before_current, current_remaining_bytes = self._local_capacity_snapshot(
            db,
            job,
            self._find_live_torrent_for_job(job),
        )
        try:
            self._move_local_job_to_nas(
                job,
                reason="manual_move_to_nas",
                free_bytes=free_bytes,
                safe_free_bytes=safe_free_bytes,
                reserved_before_current=reserved_before_current,
                current_remaining_bytes=current_remaining_bytes,
            )
            db.add(job)
            db.commit()
            db.refresh(job)
            self.process_waiting_for_local_space(db)
            db.refresh(job)
            return job
        except Exception as exc:
            self.logger.exception("Failed to move queued job %s to NAS", job.id)
            self._mark(job, "error", error=f"manual move to NAS failed: {exc}")
            db.add(job)
            db.commit()
            raise RuntimeError(str(exc)) from exc

    def suggest_final_paths(self, prefix: str | None) -> list[str]:
        roots = [Path(path).resolve() for path in self.settings.allowed_final_parent_prefixes]
        default_root = Path(self.settings.final_parent_prefix.rstrip("/")).resolve()
        raw_prefix = (prefix or "").strip()
        normalized_prefix = raw_prefix or f"{default_root}/"

        suggestions: set[str] = set()
        suggestions.update(str(root) for root in roots)

        matched_root = self._matching_final_root(normalized_prefix, roots)
        if matched_root is None:
            filtered_roots = {
                suggestion for suggestion in suggestions
                if not raw_prefix or suggestion.lower().startswith(raw_prefix.lower())
            }
            return sorted(filtered_roots or suggestions)[:50]

        browse_dir, partial = self._path_lookup_context(normalized_prefix, matched_root)
        suggestions.update(self._list_child_directories(browse_dir, partial))

        exact_dir = Path(normalized_prefix.rstrip("/"))
        if normalized_prefix and exact_dir.exists() and exact_dir.is_dir() and self._is_within_root(str(exact_dir), matched_root):
            suggestions.update(self._list_child_directories(exact_dir, ""))

        filtered_suggestions = {
            suggestion for suggestion in suggestions
            if not raw_prefix or suggestion.lower().startswith(raw_prefix.lower())
        }
        return sorted(filtered_suggestions or suggestions)[:50]

    def log_local_staging_diagnostics(self, db: Session) -> None:
        disk = shutil.disk_usage(self.settings.local_staging_root)
        safe_free_bytes = max(disk.free - self.settings.local_free_space_buffer_bytes, 0)
        directory_tree_bytes = self._directory_tree_size(self.settings.local_staging_root)
        all_torrents = self.qbt.list_torrents()
        local_torrents = [torrent for torrent in all_torrents if self._torrent_uses_local_staging(torrent)]
        local_intake_jobs = list(
            db.scalars(
                select(Job)
                .where(Job.is_terminal == False)
                .where(Job.staging_preference == "local")
                .order_by(Job.created_at.asc())
            )
        )
        queued_local_jobs = [job for job in local_intake_jobs if job.state == "waiting_for_local_space"]
        active_local_jobs = [job for job in local_intake_jobs if job.staging_actual == "local"]
        total_remaining_local_bytes = sum(
            max(self._remaining_bytes_for_torrent(torrent) or 0, 0)
            for torrent in local_torrents
        )

        self.logger.info(
            "Local staging startup diagnostics root=%s filesystem_total=%s filesystem_used=%s filesystem_free=%s "
            "safe_free=%s tree_bytes=%s local_torrents=%s intake_local_jobs=%s queued_local_jobs=%s "
            "remaining_local_bytes=%s",
            self.settings.local_staging_root,
            self._format_bytes(disk.total),
            self._format_bytes(disk.used),
            self._format_bytes(disk.free),
            self._format_bytes(safe_free_bytes),
            self._format_bytes(directory_tree_bytes),
            len(local_torrents),
            len(active_local_jobs),
            len(queued_local_jobs),
            self._format_bytes(total_remaining_local_bytes),
        )

        for torrent in local_torrents:
            torrent_hash = getattr(torrent, "hash", "") or ""
            total_size = getattr(torrent, "size", None) or getattr(torrent, "total_size", None) or 0
            remaining_bytes = max(self._remaining_bytes_for_torrent(torrent, fallback_size_bytes=total_size) or 0, 0)
            downloaded_bytes = max(total_size - remaining_bytes, 0) if total_size else 0
            progress = float(getattr(torrent, "progress", 0) or 0) * 100
            fs_share_pct = (downloaded_bytes / disk.total * 100) if disk.total > 0 else 0.0
            total_share_pct = (total_size / disk.total * 100) if disk.total > 0 and total_size > 0 else 0.0
            self.logger.info(
                "Local staging torrent hash=%s name=%r state=%s progress=%.2f%% downloaded=%s remaining=%s "
                "total=%s current_fs_share=%.3f%% full_fs_share=%.3f%% save_path=%s",
                torrent_hash[:12] or "-",
                getattr(torrent, "name", None) or "unknown",
                getattr(torrent, "state", None) or "unknown",
                progress,
                self._format_bytes(downloaded_bytes),
                self._format_bytes(remaining_bytes),
                self._format_bytes(total_size),
                fs_share_pct,
                total_share_pct,
                getattr(torrent, "save_path", None) or "-",
            )

    def enrich_jobs_with_live_stats(self, jobs: list[Job]) -> list[Job]:
        for job in jobs:
            job.progress = None
            job.eta_seconds = None
            job.download_speed_bytes_per_s = None
            job.upload_speed_bytes_per_s = None
            job.activity_summary = None

        if not jobs:
            return jobs

        try:
            torrents = self.qbt.list_torrents()
        except Exception as exc:
            self.logger.warning("Unable to enrich jobs with live qB stats: %s", exc)
            return jobs

        torrents_by_hash: dict[str, object] = {}
        torrents_by_unique_tag: dict[str, object] = {}
        for torrent in torrents:
            torrent_hash = getattr(torrent, "hash", None)
            if torrent_hash:
                torrents_by_hash[torrent_hash] = torrent
            torrent_tags = getattr(torrent, "tags", "") or ""
            for tag in torrent_tags.split(","):
                normalized = tag.strip()
                if normalized.startswith("ti_job_"):
                    torrents_by_unique_tag[normalized] = torrent

        for job in jobs:
            torrent = None
            if job.qbt_hash:
                torrent = torrents_by_hash.get(job.qbt_hash)
            if torrent is None and job.unique_tag:
                torrent = torrents_by_unique_tag.get(job.unique_tag)
            if torrent is None:
                continue

            qbt_state = getattr(torrent, "state", None)
            if qbt_state:
                job.last_seen_qbt_state = str(qbt_state)

            progress = getattr(torrent, "progress", None)
            if isinstance(progress, (int, float)):
                job.progress = float(progress)

            eta_seconds = getattr(torrent, "eta", None)
            if isinstance(eta_seconds, int) and eta_seconds >= 0:
                job.eta_seconds = eta_seconds

            download_speed = getattr(torrent, "dlspeed", None)
            if download_speed is None:
                download_speed = getattr(torrent, "dl_speed", None)
            if isinstance(download_speed, int) and download_speed >= 0:
                job.download_speed_bytes_per_s = download_speed

            upload_speed = getattr(torrent, "upspeed", None)
            if upload_speed is None:
                upload_speed = getattr(torrent, "up_speed", None)
            if isinstance(upload_speed, int) and upload_speed >= 0:
                job.upload_speed_bytes_per_s = upload_speed

            job.activity_summary = self._build_activity_summary(job, torrent)
        return jobs

    def _root_for_preference(self, preference: str) -> str:
        return self.settings.local_staging_root if preference == "local" else self.settings.nas_staging_root

    def _find_job_by_hash(self, db: Session, *, torrent_hash: str | None, exclude_job_id: str | None) -> Job | None:
        if not torrent_hash:
            return None
        jobs = list(db.scalars(select(Job).where(Job.qbt_hash == torrent_hash).order_by(Job.created_at.desc())))
        filtered = [job for job in jobs if job.id != exclude_job_id]
        if not filtered:
            return None
        for job in filtered:
            if not job.is_terminal:
                return job
        return filtered[0]

    def _duplicate_torrent_message(
        self,
        db: Session,
        *,
        torrent_hash: str | None,
        torrent_name: str | None,
        exclude_job_id: str | None,
    ) -> str:
        name_part = f"'{torrent_name}'" if torrent_name else "this torrent"
        hash_part = f" ({torrent_hash})" if torrent_hash else ""
        tracked_job = self._find_job_by_hash(db, torrent_hash=torrent_hash, exclude_job_id=exclude_job_id)
        if tracked_job is not None:
            return (
                f"{name_part}{hash_part} is already present in qBittorrent and is already tracked by intake job "
                f"{tracked_job.id}. Delete the stale intake row instead of retrying or re-adding it."
            )
        return (
            f"{name_part}{hash_part} is already present in qBittorrent. "
            "qBittorrent rejected the add because that torrent hash already exists."
        )

    def _ensure_job_can_track_torrent(self, db: Session, job: Job, torrent) -> None:
        torrent_hash = getattr(torrent, "hash", None)
        tracked_job = self._find_job_by_hash(db, torrent_hash=torrent_hash, exclude_job_id=job.id)
        if tracked_job is not None and not tracked_job.is_terminal:
            raise ValueError(
                self._duplicate_torrent_message(
                    db,
                    torrent_hash=torrent_hash,
                    torrent_name=getattr(torrent, "name", None),
                    exclude_job_id=job.id,
                )
            )

    def _empty_bulk_result(self) -> dict[str, object]:
        return {
            "requested": 0,
            "processed": 0,
            "skipped": 0,
            "failed": 0,
            "processed_ids": [],
            "skipped_ids": [],
            "failed_ids": [],
            "errors": {},
        }

    def _bulk_apply(self, job_ids: list[str], operation) -> dict[str, object]:
        unique_ids = list(dict.fromkeys(job_ids))
        result = self._empty_bulk_result()
        result["requested"] = len(unique_ids)

        for job_id in unique_ids:
            try:
                operation(job_id)
                result["processed_ids"].append(job_id)
            except (LookupError, ValueError) as exc:
                result["skipped_ids"].append(job_id)
                result["errors"][job_id] = str(exc)
            except RuntimeError as exc:
                result["failed_ids"].append(job_id)
                result["errors"][job_id] = str(exc)

        result["processed"] = len(result["processed_ids"])
        result["skipped"] = len(result["skipped_ids"])
        result["failed"] = len(result["failed_ids"])
        return result

    def _mark(self, job: Job, state: str, *, error: str | None = None) -> None:
        job.state = state
        job.updated_at = datetime.utcnow()
        job.last_error = error
        job.is_terminal = state in self.TERMINAL_STATES

    def _prepare_job_for_retry(self, job: Job) -> None:
        job.is_terminal = False
        job.last_error = None
        job.updated_at = datetime.utcnow()
        job.state = "retrying"

        # If the job failed before a successful scan/promotion, clear stale progress markers
        # so the worker doesn't jump back into a later phase with old timestamps/content paths.
        if not job.scan_completed_at:
            job.download_complete_at = None
            job.completion_event_received_at = None
            job.content_path = None
        if not job.promoted_at:
            job.scan_completed_at = None
        if not job.deleted_at:
            job.deleted_at = None

    def _sync_job_from_torrent(self, job: Job, torrent) -> None:
        if torrent is None:
            return
        job.torrent_name = getattr(torrent, "name", None) or job.torrent_name
        job.last_seen_qbt_state = getattr(torrent, "state", None)
        current_path = getattr(torrent, "content_path", None)
        if current_path:
            job.content_path = current_path
        size = getattr(torrent, "size", None) or getattr(torrent, "total_size", None)
        if isinstance(size, int):
            job.size_bytes = size

    def _state_for_retry(self, job: Job, torrent) -> str:
        if job.deleted_at:
            return "infected_deleted"
        if job.promoted_at:
            return "done"
        if job.scan_completed_at:
            return "promoting"
        if torrent is not None and self._is_torrent_complete(torrent):
            if not job.download_complete_at:
                job.download_complete_at = datetime.utcnow()
            return "download_complete"
        return "downloading"

    def _resolve_hash_for_job(self, db: Session, job: Job) -> None:
        torrent = self.qbt.find_by_unique_tag(job.unique_tag)
        if torrent is None:
            self._mark(job, "waiting_for_qbt_hash")
        else:
            job.qbt_hash = getattr(torrent, "hash", None)
            job.torrent_name = getattr(torrent, "name", None)
            job.last_seen_qbt_state = getattr(torrent, "state", None)
            size = getattr(torrent, "size", None) or getattr(torrent, "total_size", None)
            if isinstance(size, int):
                job.size_bytes = size
            self._mark(job, "downloading")
        db.add(job)
        db.commit()
        db.refresh(job)

    def _find_live_torrent_for_job(self, job: Job):
        current_hash = job.qbt_hash
        torrent = self.qbt.get_torrent(current_hash) if current_hash else None
        if torrent is not None:
            return torrent

        if job.unique_tag:
            torrent = self.qbt.find_by_unique_tag(job.unique_tag)
            if torrent is not None:
                self._rebind_job_hash(job, torrent, source="unique_tag")
                return torrent

        torrent = self.qbt.find_existing_from_magnet(job.magnet_uri)
        if torrent is not None:
            self._rebind_job_hash(job, torrent, source="magnet_uri")
            return torrent

        return None

    def _rebind_job_hash(self, job: Job, torrent, *, source: str) -> None:
        old_hash = job.qbt_hash
        new_hash = getattr(torrent, "hash", None)
        if new_hash and new_hash != old_hash:
            self.logger.warning(
                "Recovered qB tracking for job %s via %s old_hash=%s new_hash=%s unique_tag=%s",
                job.id,
                source,
                old_hash or "-",
                new_hash,
                job.unique_tag,
            )
            job.qbt_hash = new_hash
        self._sync_job_from_torrent(job, torrent)

    def ingest_completion_event(self, db: Session, *, qbt_hash: str | None, qbt_hash_v2: str | None,
                                unique_tag: str | None, tags: str | None, torrent_name: str | None,
                                content_path: str | None, root_path: str | None,
                                save_path: str | None, size_bytes: int | None) -> Job | None:
        unique_tag = unique_tag or self._extract_unique_tag(tags)
        job = None
        if qbt_hash:
            job = db.scalar(select(Job).where(Job.qbt_hash == qbt_hash))
        if not job and unique_tag:
            job = db.scalar(select(Job).where(Job.unique_tag == unique_tag))
        if not job:
            self.logger.warning(
                "Completion event ignored: no matching job found qbt_hash=%s qbt_hash_v2=%s unique_tag=%s tags=%s torrent_name=%s",
                qbt_hash,
                qbt_hash_v2,
                unique_tag,
                tags,
                torrent_name,
            )
            return None
        if qbt_hash and job.qbt_hash != qbt_hash:
            self.logger.warning(
                "Rebinding job %s from completion event old_hash=%s new_hash=%s unique_tag=%s",
                job.id,
                job.qbt_hash or "-",
                qbt_hash,
                unique_tag or job.unique_tag,
            )
            job.qbt_hash = qbt_hash

        # Backdate by grace seconds so an event-triggered processing pass can act immediately.
        job.completion_event_received_at = datetime.utcnow() - timedelta(seconds=self.settings.completion_grace_seconds)
        if torrent_name:
            job.torrent_name = torrent_name
        event_path = content_path or root_path or save_path
        if event_path:
            job.content_path = event_path
        if isinstance(size_bytes, int) and size_bytes > 0:
            job.size_bytes = size_bytes
        self._mark(job, "completion_event_received")
        db.add(job)
        db.commit()
        db.refresh(job)
        return job

    def process_job_immediately(self, db: Session, *, job_id: str, ignore_event_grace: bool = False) -> Job:
        job = db.get(Job, job_id)
        if not job:
            raise LookupError("Job not found")
        if job.is_terminal:
            return job
        try:
            was_local_staging = job.staging_actual == "local"
            self._process_one(db, job, ignore_event_grace=ignore_event_grace)
            db.refresh(job)
            if was_local_staging and job.is_terminal:
                self.process_waiting_for_local_space(db)
                db.refresh(job)
            return job
        except Exception as exc:
            self.logger.exception("Immediate processing failed for job %s", job.id)
            self._mark(job, "error", error=str(exc))
            db.add(job)
            db.commit()
            db.refresh(job)
            raise RuntimeError(str(exc)) from exc

    def process_nonterminal_jobs(self, db: Session) -> None:
        jobs = list(db.scalars(select(Job).where(Job.is_terminal == False).order_by(Job.created_at.asc())))
        for job in jobs:
            try:
                self._process_one(db, job, ignore_event_grace=False)
            except Exception as exc:
                self.logger.exception("Worker failed for job %s", job.id)
                self._mark(job, "error", error=str(exc))
                db.add(job)
                db.commit()

    def process_waiting_for_local_space(self, db: Session) -> None:
        jobs = list(
            db.scalars(
                select(Job)
                .where(Job.is_terminal == False)
                .where(Job.state == "waiting_for_local_space")
                .order_by(Job.created_at.asc())
            )
        )
        for job in jobs:
            try:
                self._process_one(db, job, ignore_event_grace=False)
            except Exception as exc:
                self.logger.exception("Queued local-space job failed for job %s", job.id)
                self._mark(job, "error", error=str(exc))
                db.add(job)
                db.commit()

    def _process_one(self, db: Session, job: Job, *, ignore_event_grace: bool) -> None:
        if not job.qbt_hash:
            self._resolve_hash_for_job(db, job)
            return

        torrent = self._find_live_torrent_for_job(job)
        if torrent is None:
            if job.state in {"infected_deleted", "done"}:
                job.is_terminal = True
                db.add(job)
                db.commit()
                return
            self.logger.warning(
                "Unable to find live qB torrent for job %s stored_hash=%s unique_tag=%s",
                job.id,
                job.qbt_hash,
                job.unique_tag,
            )
            raise RuntimeError(f"Torrent {job.qbt_hash} not found in qBittorrent")
        self._ensure_job_can_track_torrent(db, job, torrent)

        qbt_state = getattr(torrent, "state", None)
        self._sync_job_from_torrent(job, torrent)
        self._raise_for_qbt_error_state(torrent)

        self._apply_local_staging_policy(db, job, torrent)

        is_complete = self._is_torrent_complete(torrent)
        if is_complete and not job.download_complete_at:
            job.download_complete_at = datetime.utcnow()
            self._mark(job, "download_complete")

        event_ready = ignore_event_grace or (
            job.completion_event_received_at
            and datetime.utcnow() >= job.completion_event_received_at + timedelta(seconds=self.settings.completion_grace_seconds)
        )
        if (job.download_complete_at and job.state in {"download_complete", "completion_event_received", "downloading", "scanning"}) and (event_ready or not job.completion_event_received_at):
            if not self._try_acquire_long_running_job(job.id):
                self.logger.info("Skipping duplicate scan/finalize attempt for job %s state=%s", job.id, job.state)
                return
            try:
                self._mark(job, "scanning")
                db.add(job)
                db.commit()
                db.refresh(job)
                self._scan_and_finalize(job)
            finally:
                self._release_long_running_job(job.id)

        db.add(job)
        db.commit()

    def _evaluate_staging_now(self, db: Session, job: Job) -> None:
        if not job.qbt_hash:
            return
        torrent = self._find_live_torrent_for_job(job)
        if torrent is None:
            return
        self._sync_job_from_torrent(job, torrent)
        self._apply_local_staging_policy(db, job, torrent)
        db.add(job)
        db.commit()
        db.refresh(job)

    def _remaining_bytes_for_torrent(self, torrent, *, fallback_size_bytes: int | None = None) -> int | None:
        amount_left = getattr(torrent, "amount_left", None)
        if isinstance(amount_left, int) and amount_left >= 0:
            return amount_left

        total_size = getattr(torrent, "size", None) or getattr(torrent, "total_size", None) or fallback_size_bytes
        if not isinstance(total_size, int) or total_size <= 0:
            return self.settings.local_max_bytes

        progress = float(getattr(torrent, "progress", 0) or 0)
        if progress <= 0:
            return total_size
        if progress >= 1.0:
            return 0
        return max(int(total_size * (1.0 - progress)), 0)

    def _directory_tree_size(self, root_path: str) -> int:
        total_bytes = 0
        for current_root, _, filenames in os.walk(root_path):
            for filename in filenames:
                file_path = os.path.join(current_root, filename)
                try:
                    if os.path.islink(file_path):
                        continue
                    total_bytes += os.path.getsize(file_path)
                except OSError:
                    continue
        return total_bytes

    @staticmethod
    def _format_bytes(value: int | None) -> str:
        if value is None:
            return "-"
        units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
        size = float(value)
        unit_index = 0
        while size >= 1024 and unit_index < len(units) - 1:
            size /= 1024
            unit_index += 1
        if unit_index == 0:
            return f"{int(size)} {units[unit_index]}"
        return f"{size:.2f} {units[unit_index]}"

    @staticmethod
    def _format_eta(seconds: int | None) -> str | None:
        if seconds is None or seconds < 0:
            return None
        if seconds >= 8640000:
            return "infinite"
        days, remainder = divmod(seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, secs = divmod(remainder, 60)
        parts: list[str] = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        if secs and not parts:
            parts.append(f"{secs}s")
        if not parts:
            return "0s"
        return " ".join(parts[:2])

    def _build_activity_summary(self, job: Job, torrent) -> str | None:
        qbt_state = str(getattr(torrent, "state", None) or job.last_seen_qbt_state or job.state or "")
        is_complete = isinstance(job.progress, float) and job.progress >= 1.0
        eta_is_infinite = isinstance(job.eta_seconds, int) and job.eta_seconds >= 8640000
        upload_states = {"uploading", "stalledUP", "forcedUP", "pausedUP", "queuedUP"}
        parts: list[str] = []
        if qbt_state in self.QBT_ATTENTION_STATES or eta_is_infinite:
            parts.append(f"qB {qbt_state}")
        if isinstance(job.progress, float):
            parts.append(f"{job.progress * 100:.2f}%")
        if isinstance(job.download_speed_bytes_per_s, int) and not is_complete:
            parts.append(f"{self._format_bytes(job.download_speed_bytes_per_s)}/s")
        eta_text = self._format_eta(job.eta_seconds) if not is_complete else None
        if eta_text:
            parts.append(f"ETA {eta_text}")
        if is_complete and qbt_state in upload_states:
            parts.append("Seeding")
        if is_complete and isinstance(job.upload_speed_bytes_per_s, int) and job.upload_speed_bytes_per_s > 0:
            parts.append(f"Up {self._format_bytes(job.upload_speed_bytes_per_s)}/s")
        if parts:
            return " | ".join(parts)
        if qbt_state:
            return f"qB {qbt_state}"
        return None

    def _raise_for_qbt_error_state(self, torrent) -> None:
        qbt_state = str(getattr(torrent, "state", "") or "")
        if qbt_state not in self.QBT_ERROR_STATES:
            return
        torrent_name = getattr(torrent, "name", None) or "unknown torrent"
        torrent_hash = getattr(torrent, "hash", None) or "unknown hash"
        raise RuntimeError(
            f"qBittorrent torrent '{torrent_name}' ({torrent_hash}) is in state '{qbt_state}'. "
            "Fix it in qBittorrent or delete the qBittorrent torrent, then retry the intake job."
        )

    def _try_acquire_long_running_job(self, job_id: str) -> bool:
        with _ACTIVE_LONG_RUNNING_JOBS_LOCK:
            if job_id in _ACTIVE_LONG_RUNNING_JOBS:
                return False
            _ACTIVE_LONG_RUNNING_JOBS.add(job_id)
            return True

    def _release_long_running_job(self, job_id: str) -> None:
        with _ACTIVE_LONG_RUNNING_JOBS_LOCK:
            _ACTIVE_LONG_RUNNING_JOBS.discard(job_id)

    def _path_within_local_staging(self, path_value: str | None) -> bool:
        if not path_value:
            return False
        local_root = self.settings.local_staging_root.rstrip("/")
        normalized = path_value.rstrip("/")
        if normalized == local_root or normalized.startswith(f"{local_root}/"):
            return True
        try:
            local_root_path = Path(local_root).resolve()
            candidate = Path(normalized).resolve()
            return candidate == local_root_path or local_root_path in candidate.parents
        except OSError:
            return False

    def _torrent_uses_local_staging(self, torrent) -> bool:
        save_path = getattr(torrent, "save_path", None)
        content_path = getattr(torrent, "content_path", None)
        return self._path_within_local_staging(save_path) or self._path_within_local_staging(content_path)

    def _local_capacity_snapshot(self, db: Session, current_job: Job, current_torrent) -> tuple[int, int, int, int]:
        local_jobs = list(
            db.scalars(
                select(Job)
                .where(Job.is_terminal == False)
                .where(Job.staging_preference == "local")
                .where(Job.staging_actual == "local")
                .where(Job.qbt_hash.is_not(None))
                .order_by(Job.created_at.asc())
            )
        )
        all_torrents = self.qbt.list_torrents()
        torrents_by_hash = {getattr(torrent, "hash", None): torrent for torrent in all_torrents}
        if current_job.qbt_hash:
            torrents_by_hash[current_job.qbt_hash] = current_torrent
        local_job_hashes = {job.qbt_hash for job in local_jobs if job.qbt_hash}

        free_bytes = shutil.disk_usage(self.settings.local_staging_root).free
        safe_free_bytes = max(free_bytes - self.settings.local_free_space_buffer_bytes, 0)
        reserved_before_current = 0
        current_remaining_bytes = 0
        for torrent in all_torrents:
            torrent_hash = getattr(torrent, "hash", None)
            if torrent_hash in local_job_hashes:
                continue
            if not self._torrent_uses_local_staging(torrent):
                continue
            remaining_bytes = self._remaining_bytes_for_torrent(torrent)
            if remaining_bytes is None or remaining_bytes <= 0:
                continue
            reserved_before_current += remaining_bytes

        for local_job in local_jobs:
            torrent = torrents_by_hash.get(local_job.qbt_hash)
            remaining_bytes = self._remaining_bytes_for_torrent(torrent, fallback_size_bytes=local_job.size_bytes)
            if remaining_bytes is None or remaining_bytes <= 0:
                continue
            if local_job.id == current_job.id:
                current_remaining_bytes = remaining_bytes
                break
            reserved_before_current += remaining_bytes

        return free_bytes, safe_free_bytes, reserved_before_current, current_remaining_bytes

    def _move_local_job_to_nas(self, job: Job, *, reason: str, free_bytes: int, safe_free_bytes: int,
                               reserved_before_current: int, current_remaining_bytes: int) -> None:
        self.logger.info(
            "Moving job %s to NAS staging reason=%s free_bytes=%s safe_free_bytes=%s reserved_before_current=%s current_remaining_bytes=%s",
            job.id,
            reason,
            free_bytes,
            safe_free_bytes,
            reserved_before_current,
            current_remaining_bytes,
        )
        self.qbt.pause(job.qbt_hash)
        self.qbt.set_save_path(job.qbt_hash, self.settings.nas_staging_root)
        self.qbt.resume(job.qbt_hash)
        job.staging_actual = "nas"
        job.staging_root_actual = self.settings.nas_staging_root
        job.staging_overridden = True
        job.override_reason = reason
        self._mark(job, "downloading")

    def _queue_local_job_for_space(self, job: Job, *, reason: str, free_bytes: int, safe_free_bytes: int,
                                   reserved_before_current: int, current_remaining_bytes: int) -> None:
        self.logger.info(
            "Queueing job %s for local staging space reason=%s free_bytes=%s safe_free_bytes=%s reserved_before_current=%s current_remaining_bytes=%s",
            job.id,
            reason,
            free_bytes,
            safe_free_bytes,
            reserved_before_current,
            current_remaining_bytes,
        )
        self.qbt.pause(job.qbt_hash)
        job.staging_actual = "local"
        job.staging_root_actual = self.settings.local_staging_root
        job.staging_overridden = False
        job.override_reason = None
        self._mark(job, "waiting_for_local_space")

    def _resume_local_job_from_queue(self, job: Job) -> None:
        self.logger.info("Resuming job %s after local staging space became available", job.id)
        self.qbt.resume(job.qbt_hash)
        self._mark(job, "downloading")

    def _apply_local_staging_policy(self, db: Session, job: Job, torrent) -> None:
        if job.staging_preference != "local":
            return
        if job.staging_actual != "local":
            return
        qbt_state = str(getattr(torrent, "state", "") or "")
        if job.size_bytes is not None and job.size_bytes > self.settings.local_max_bytes:
            free_bytes = shutil.disk_usage(self.settings.local_staging_root).free
            self._move_local_job_to_nas(
                job,
                reason="size_exceeds_threshold",
                free_bytes=free_bytes,
                safe_free_bytes=max(free_bytes - self.settings.local_free_space_buffer_bytes, 0),
                reserved_before_current=0,
                current_remaining_bytes=job.size_bytes,
            )
            return

        free_bytes, safe_free_bytes, reserved_before_current, current_remaining_bytes = self._local_capacity_snapshot(
            db,
            job,
            torrent,
        )
        if current_remaining_bytes <= 0:
            return
        self.logger.debug(
            "Local capacity check job=%s free_bytes=%s safe_free_bytes=%s reserved_before_current=%s current_remaining_bytes=%s",
            job.id,
            free_bytes,
            safe_free_bytes,
            reserved_before_current,
            current_remaining_bytes,
        )
        if reserved_before_current + current_remaining_bytes <= safe_free_bytes:
            if job.state == "waiting_for_local_space":
                self._resume_local_job_from_queue(job)
            return

        if self.settings.local_overflow_policy == "nas":
            self._move_local_job_to_nas(
                job,
                reason="insufficient_local_space",
                free_bytes=free_bytes,
                safe_free_bytes=safe_free_bytes,
                reserved_before_current=reserved_before_current,
                current_remaining_bytes=current_remaining_bytes,
            )
            return

        if job.state == "waiting_for_local_space" and qbt_state in {"pausedDL", "pausedUP"}:
            return

        self._queue_local_job_for_space(
            job,
            reason="insufficient_local_space",
            free_bytes=free_bytes,
            safe_free_bytes=safe_free_bytes,
            reserved_before_current=reserved_before_current,
            current_remaining_bytes=current_remaining_bytes,
        )

    def _scan_and_finalize(self, job: Job) -> None:
        if not job.content_path:
            torrent = self._find_live_torrent_for_job(job)
            job.content_path = getattr(torrent, "content_path", None) or job.content_path
        if not job.content_path:
            raise RuntimeError("content_path is not available for completed torrent")

        self.qbt.pause(job.qbt_hash)
        self.logger.info("Scanning job %s path=%s", job.id, job.content_path)
        scan_started = time.monotonic()
        result = self.scanner.scan_path(job.content_path)
        scan_duration_seconds = time.monotonic() - scan_started
        job.scan_completed_at = datetime.utcnow()
        self.logger.info(
            "Scan finished for job %s clean=%s infected=%s duration_seconds=%.2f",
            job.id,
            result.clean,
            result.infected,
            scan_duration_seconds,
        )

        if result.infected:
            threat = result.threat_name or "unknown"
            self.qbt.delete_with_files(job.qbt_hash)
            job.threat_name = threat
            job.deleted_at = datetime.utcnow()
            self.telegram.send_infected_deleted(
                torrent_name=job.torrent_name,
                qbt_hash=job.qbt_hash,
                staging_path=job.content_path,
                final_parent=job.final_parent,
                threat_name=threat,
            )
            self._mark(job, "infected_deleted")
            self.logger.warning("Infected job deleted: job=%s threat=%s", job.id, threat)
            return

        self._mark(job, "promoting")
        self.logger.info("Promoting clean job %s to %s", job.id, job.final_parent)
        self.qbt.set_location(job.qbt_hash, job.final_parent)
        if job.final_category:
            resolved_category = self.qbt.resolve_or_create_category(
                job.final_category,
                create_if_missing=self.settings.auto_create_final_category,
            )
            if resolved_category != job.final_category:
                self.logger.info(
                    "Mapped final category for job %s from '%s' to existing '%s'",
                    job.id,
                    job.final_category,
                    resolved_category,
                )
            job.final_category = resolved_category
            self.qbt.set_category(job.qbt_hash, resolved_category)
        self.qbt.resume(job.qbt_hash)
        job.promoted_at = datetime.utcnow()
        self._mark(job, "done")
        self.logger.info("Job %s complete and resumed for seeding", job.id)

    def _is_torrent_complete(self, torrent) -> bool:
        progress = float(getattr(torrent, "progress", 0) or 0)
        amount_left = getattr(torrent, "amount_left", None)
        completion_on = getattr(torrent, "completion_on", 0) or 0
        qbt_state = str(getattr(torrent, "state", "") or "")
        state_enum = getattr(torrent, "state_enum", None)
        self.logger.info(
            "Completion check hash=%s state=%s progress=%.5f amount_left=%s completion_on=%s",
            getattr(torrent, "hash", "unknown"),
            qbt_state,
            progress,
            amount_left,
            completion_on,
        )

        if state_enum is not None:
            if getattr(state_enum, "is_downloading", False) or getattr(state_enum, "is_checking", False):
                return False
            if qbt_state in {"moving", "allocating", "missingFiles", "error", "unknown"}:
                return False

        if isinstance(amount_left, int) and amount_left > 0:
            return False

        if progress < 1.0:
            return False

        not_ready_states = {
            "downloading",
            "stalledDL",
            "forcedDL",
            "metaDL",
            "forcedMetaDL",
            "checkingDL",
            "checkingResumeData",
        }
        if qbt_state in not_ready_states:
            return False

        return bool(completion_on or progress >= 1.0)

    def _extract_unique_tag(self, tags: str | None) -> str | None:
        if not tags:
            return None
        for raw_tag in tags.split(","):
            tag = raw_tag.strip()
            if tag.startswith("ti_job_"):
                return tag
        return None

    def _path_lookup_context(self, typed_path: str, root: Path) -> tuple[Path, str]:
        if typed_path.endswith("/"):
            candidate = Path(typed_path.rstrip("/"))
            partial = ""
        else:
            candidate = Path(typed_path).parent
            partial = Path(typed_path).name

        browse_dir = candidate
        while browse_dir != root and (not browse_dir.exists() or not browse_dir.is_dir()):
            browse_dir = browse_dir.parent

        if not browse_dir.exists() or not browse_dir.is_dir():
            browse_dir = root

        return browse_dir, partial

    def _matching_final_root(self, typed_path: str, roots: list[Path]) -> Path | None:
        typed = typed_path.rstrip("/")
        matches = [
            root for root in roots
            if typed == str(root) or typed.startswith(f"{root}/")
        ]
        if not matches:
            return None
        return max(matches, key=lambda root: len(str(root)))

    def _list_child_directories(self, directory: Path, partial: str) -> set[str]:
        matches: set[str] = set()
        if not directory.exists() or not directory.is_dir():
            return matches

        lowered_partial = partial.lower()
        try:
            for entry in directory.iterdir():
                if not entry.is_dir():
                    continue
                if lowered_partial and not entry.name.lower().startswith(lowered_partial):
                    continue
                matches.add(str(entry))
        except OSError as exc:
            self.logger.warning("Failed to list suggestion directory %s: %s", directory, exc)
        return matches

    def _is_within_root(self, candidate: str, root: Path) -> bool:
        try:
            resolved = Path(candidate.rstrip("/")).resolve()
            return resolved == root or root in resolved.parents
        except OSError:
            return False
