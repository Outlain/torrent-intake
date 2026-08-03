from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from statistics import fmean
from uuid import uuid4

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from .config import get_settings
from .db import SessionLocal
from .event_writer import emit_event
from .models import Job, ScanFile, ScanRun, ScannerControl
from .qbt import QbtService
from .torrent_guard import TorrentSafetyGuard
from .scanner import (
    ScanInterrupted,
    ScannerDefinitionsStale,
    ScannerIdentity,
    ScannerPolicyError,
    ScannerService,
    ScannerUnavailable,
    parse_scanner_version,
)


SCAN_QUEUE_STATES = {"scan_pending", "scanning", "scan_paused"}
SCAN_ACTION_STATES = {
    "scan_clean",
    "promoting",
    "scan_infected",
    "quarantining_infected",
    "deleting_infected",
}


@dataclass(frozen=True)
class ScanClaim:
    job_id: str
    worker_id: str
    is_large: bool


class ScanCoordinator:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.qbt = QbtService()
        self.scanner = ScannerService()
        self.guard = TorrentSafetyGuard()
        self.logger = logging.getLogger(__name__)

    def ensure_control(self, db: Session) -> ScannerControl:
        control = db.get(ScannerControl, 1)
        if control is None:
            control = ScannerControl(
                id=1,
                requested_slots=self._default_slots(),
                boost_until_queue_empty=False,
                updated_at=datetime.utcnow(),
            )
            db.add(control)
            db.commit()
            db.refresh(control)
            return control

        clamped_slots = min(max(control.requested_slots, 1), self._hard_max_slots())
        if control.requested_slots != clamped_slots:
            control.requested_slots = clamped_slots
            control.boost_until_queue_empty = clamped_slots > self._default_slots()
            control.updated_at = datetime.utcnow()
            db.add(control)
            db.commit()
            db.refresh(control)
        return control

    def queue_job(self, db: Session, job: Job) -> ScanRun:
        now = datetime.utcnow()
        run = db.get(ScanRun, job.id)
        if run is None:
            run = ScanRun(job_id=job.id)
            db.add(run)

        run.pause_requested = False
        run.failure_count = 0
        run.queued_at = run.queued_at or now
        run.next_attempt_at = None
        run.worker_id = None
        run.lease_expires_at = None
        run.heartbeat_at = None
        run.current_file = None
        run.current_file_started_at = None
        run.verdict = None
        run.last_error = None
        run.updated_at = now

        job.state = "scan_pending"
        job.is_terminal = False
        job.last_error = None
        job.updated_at = now
        return run

    def recover_interrupted_scans(self, db: Session, *, force: bool = False) -> None:
        self.ensure_control(db)
        now = datetime.utcnow()
        jobs = list(
            db.scalars(
                select(Job).where(Job.state.in_(tuple(SCAN_QUEUE_STATES | SCAN_ACTION_STATES)))
            )
        )

        for job in jobs:
            run = db.get(ScanRun, job.id)
            if run is None:
                run = ScanRun(job_id=job.id, queued_at=now, updated_at=now)
                db.add(run)

            if job.state == "scanning" and (
                force
                or not run.worker_id
                or not run.lease_expires_at
                or run.lease_expires_at <= now
            ):
                job.state = "scan_paused" if run.pause_requested else "scan_pending"
                job.updated_at = now
                run.queued_at = now
                run.worker_id = None
                run.lease_expires_at = None
                run.heartbeat_at = None
                run.current_file = None
                run.current_file_started_at = None
                run.updated_at = now
                db.execute(
                    update(ScanFile)
                    .where(ScanFile.job_id == job.id, ScanFile.status == "scanning")
                    .values(
                        status="pending",
                        scan_started_at=None,
                        scan_duration_seconds=None,
                    )
                )
            elif job.state == "scan_pending" and run.queued_at is None:
                run.queued_at = now

        db.commit()

    def claim_jobs(self, db: Session, scheduler_id: str) -> list[ScanClaim]:
        self.recover_interrupted_scans(db)
        now = datetime.utcnow()
        control = self.ensure_control(db)
        if control.maintenance_mode:
            return []
        if not self.scanner.health().can_scan:
            return []
        active_rows = list(
            db.execute(
                select(Job, ScanRun)
                .join(ScanRun, ScanRun.job_id == Job.id)
                .where(
                    Job.state == "scanning",
                    ScanRun.lease_expires_at.is_not(None),
                    ScanRun.lease_expires_at > now,
                )
            )
        )
        active_count = len(active_rows)
        active_large = sum(
            1 for job, run in active_rows
            if self._is_large(job, run)
        )
        queued_count = db.scalar(
            select(func.count()).select_from(Job).where(Job.state == "scan_pending")
        ) or 0
        if queued_count == 0 and control.boost_until_queue_empty:
            control.requested_slots = self._default_slots()
            control.boost_until_queue_empty = False
            control.updated_at = datetime.utcnow()
            db.add(control)
            db.commit()

        available = max(control.requested_slots - active_count, 0)
        if available == 0:
            return []

        candidates = list(
            db.execute(
                select(Job, ScanRun)
                .join(ScanRun, ScanRun.job_id == Job.id)
                .where(
                    Job.state == "scan_pending",
                    ScanRun.pause_requested == False,
                    or_(ScanRun.next_attempt_at.is_(None), ScanRun.next_attempt_at <= now),
                )
            )
        )
        candidates.sort(key=lambda item: self._queue_sort_key(item[0], item[1], now))

        claims: list[ScanClaim] = []
        for job, run in candidates:
            if len(claims) >= available:
                break
            is_large = self._is_large(job, run)
            if is_large and active_large >= self._max_large_scans():
                continue

            worker_id = f"{scheduler_id}:{uuid4().hex[:12]}"
            claimed = db.execute(
                update(Job)
                .where(Job.id == job.id, Job.state == "scan_pending")
                .values(state="scanning", updated_at=now, last_error=None, is_terminal=False)
            )
            if claimed.rowcount != 1:
                db.rollback()
                continue

            run.worker_id = worker_id
            run.started_at = now
            run.heartbeat_at = now
            run.lease_expires_at = now + timedelta(seconds=self._lease_seconds())
            run.next_attempt_at = None
            run.current_file = None
            run.current_file_started_at = None
            run.attempts += 1
            run.updated_at = now
            db.add(run)
            db.commit()
            claims.append(ScanClaim(job_id=job.id, worker_id=worker_id, is_large=is_large))
            if is_large:
                active_large += 1

        queued_left = db.scalar(select(func.count()).select_from(Job).where(Job.state == "scan_pending")) or 0
        if queued_left == 0 and control.boost_until_queue_empty:
            control.requested_slots = self._default_slots()
            control.boost_until_queue_empty = False
            control.updated_at = datetime.utcnow()
            db.add(control)
            db.commit()

        return claims

    def run_claim(self, claim: ScanClaim, stop_event: threading.Event) -> None:
        with SessionLocal() as db:
            job = db.get(Job, claim.job_id)
            run = db.get(ScanRun, claim.job_id)
            if not job or not run or job.state != "scanning" or run.worker_id != claim.worker_id:
                return

            try:
                identity = self.scanner.require_healthy(force=True)
                torrent = self._find_torrent(job)
                if torrent is None:
                    raise RuntimeError("qBittorrent torrent is temporarily unavailable")

                torrent_hash = self.guard.validate_common(
                    db, job, torrent, require_paused=False
                )
                job.qbt_hash = torrent_hash
                job.last_seen_qbt_state = getattr(torrent, "state", None)
                db.add(job)
                db.commit()

                self.qbt.pause(torrent_hash)
                torrent = self._wait_for_paused_torrent(db, job, claim, stop_event)
                content_path = str(
                    self.guard.validate_staging(db, job, torrent, require_paused=True)
                )
                job.content_path = content_path
                job.last_seen_qbt_state = getattr(torrent, "state", None)
                db.add(job)
                db.commit()
                self._prepare_manifest(
                    db,
                    job,
                    run,
                    content_path,
                    identity,
                    heartbeat=lambda: self._heartbeat(claim, stop_event),
                )

                scanned_this_claim = 0
                while True:
                    db.expire_all()
                    job = db.get(Job, claim.job_id)
                    run = db.get(ScanRun, claim.job_id)
                    if not job or not run or run.worker_id != claim.worker_id:
                        raise ScanInterrupted("scan tracking was removed or reclaimed")
                    if stop_event.is_set():
                        raise ScanInterrupted("scanner is shutting down")
                    if run.pause_requested:
                        job.state = "scan_paused"
                        job.updated_at = datetime.utcnow()
                        run.current_file = None
                        run.current_file_started_at = None
                        run.updated_at = datetime.utcnow()
                        self._release_lease(run)
                        db.add_all([job, run])
                        db.commit()
                        return
                    if self._maintenance_requested(db):
                        self._defer_claim(
                            db,
                            claim,
                            "Scanner maintenance requested; scan drained after the current file.",
                        )
                        return

                    identity = self.scanner.require_healthy(force=True)
                    if self._identity_requires_reset(run, identity):
                        self._prepare_manifest(
                            db,
                            job,
                            run,
                            content_path,
                            identity,
                            heartbeat=lambda: self._heartbeat(claim, stop_event),
                        )
                        continue
                    self._sync_run_identity(run, identity)
                    db.add(run)
                    db.commit()

                    scan_file = db.scalar(
                        select(ScanFile)
                        .where(ScanFile.job_id == job.id, ScanFile.status == "pending")
                        .order_by(ScanFile.size_bytes.asc(), ScanFile.id.asc())
                        .limit(1)
                    )
                    if scan_file is None:
                        # Rebuild once after all checkpoints are clean so files added or
                        # changed during a long scan cannot bypass the promotion gate.
                        self._prepare_manifest(
                            db,
                            job,
                            run,
                            content_path,
                            identity,
                            heartbeat=lambda: self._heartbeat(claim, stop_event),
                        )
                        db.expire_all()
                        job = db.get(Job, claim.job_id)
                        run = db.get(ScanRun, claim.job_id)
                        if not job or not run or run.worker_id != claim.worker_id:
                            raise ScanInterrupted("scan tracking was removed or reclaimed")
                        if run.pause_requested:
                            continue
                        pending_files = db.scalar(
                            select(func.count())
                            .select_from(ScanFile)
                            .where(ScanFile.job_id == job.id, ScanFile.status == "pending")
                        ) or 0
                        if pending_files:
                            continue
                        torrent = self._find_torrent(job)
                        if torrent is None:
                            raise RuntimeError("qBittorrent torrent disappeared before clean-scan completion")
                        verified_content = self.guard.validate_staging(
                            db, job, torrent, require_paused=True
                        )
                        if Path(run.root_path or "").resolve() != verified_content:
                            raise RuntimeError("torrent content path changed during scanning")
                        self._finish_clean_scan(db, job, run)
                        return

                    absolute_path = self._absolute_scan_path(run.root_path, scan_file.relative_path)
                    previous_size = scan_file.size_bytes
                    self._refresh_file_fingerprint(scan_file, absolute_path)
                    run.total_bytes += scan_file.size_bytes - previous_size
                    scan_file.status = "scanning"
                    scan_file.attempts += 1
                    scan_file.last_error = None
                    scan_file.scan_started_at = datetime.utcnow()
                    run.current_file = scan_file.relative_path
                    run.current_file_started_at = scan_file.scan_started_at
                    run.heartbeat_at = scan_file.scan_started_at
                    run.lease_expires_at = run.heartbeat_at + timedelta(seconds=self._lease_seconds())
                    run.updated_at = run.heartbeat_at
                    db.add_all([scan_file, run])
                    db.commit()

                    result = self.scanner.scan_path(
                        str(absolute_path),
                        identity=identity,
                        heartbeat=lambda: self._heartbeat(claim, stop_event),
                        should_stop=stop_event.is_set,
                        expected_file_identity=(
                            int(scan_file.device),
                            int(scan_file.inode),
                            scan_file.size_bytes,
                            scan_file.mtime_ns,
                            int(scan_file.ctime_ns),
                        ),
                    )

                    db.expire_all()
                    job = db.get(Job, claim.job_id)
                    run = db.get(ScanRun, claim.job_id)
                    scan_file = db.get(ScanFile, scan_file.id)
                    if not job or not run or not scan_file or run.worker_id != claim.worker_id:
                        raise ScanInterrupted("scan tracking was removed or reclaimed")

                    scan_file.status = "infected" if result.infected else "clean"
                    scan_file.threat_name = result.threat_name
                    scan_file.scanner_version = result.identity.raw_version
                    scan_file.engine_version = result.identity.engine_version
                    scan_file.database_version = result.identity.database_version
                    scan_file.database_updated_at = result.identity.database_updated_at
                    scan_file.policy_version = result.identity.policy_version
                    scan_file.scan_started_at = result.scan_started_at
                    scan_file.scan_duration_seconds = result.duration_seconds
                    scan_file.scanned_at = datetime.utcnow()
                    scan_file.last_error = None
                    run.completed_files += 1
                    run.completed_bytes += scan_file.size_bytes
                    run.current_file = None
                    run.current_file_started_at = None
                    self._sync_run_identity(run, result.identity)
                    run.failure_count = 0
                    run.last_error = None
                    run.updated_at = datetime.utcnow()
                    db.add_all([scan_file, run])

                    if result.infected:
                        run.verdict = "infected"
                        job.threat_name = result.threat_name or "unknown"
                        job.scan_completed_at = datetime.utcnow()
                        job.state = "scan_infected"
                        job.updated_at = datetime.utcnow()
                        self._release_lease(run)
                        emit_event(
                            "threat_detected",
                            "critical",
                            "Malware detected in torrent intake",
                            event_id=f"{job.id}-threat-{scan_file.id}",
                            source_path=str(absolute_path),
                            threat_name=job.threat_name,
                            action_success=False,
                            job_id=job.id,
                            torrent_hash=job.qbt_hash,
                        )
                        db.add_all([job, run])
                        db.commit()
                        return

                    db.commit()
                    scanned_this_claim += 1

                    db.refresh(run)
                    if run.pause_requested:
                        job.state = "scan_paused"
                        job.updated_at = datetime.utcnow()
                        self._release_lease(run)
                        db.add_all([job, run])
                        db.commit()
                        return

                    if (
                        scanned_this_claim >= max(self.settings.scan_yield_after_files, 1)
                        and self._should_yield(db, job, run, claim.is_large)
                    ):
                        job.state = "scan_pending"
                        job.updated_at = datetime.utcnow()
                        run.queued_at = datetime.utcnow()
                        self._release_lease(run)
                        db.add_all([job, run])
                        db.commit()
                        return
            except (ScannerUnavailable, ScannerDefinitionsStale) as exc:
                self._defer_claim(db, claim, str(exc), retry_after_seconds=30)
            except ScannerPolicyError as exc:
                self.logger.error("Scanner policy rejected job %s: %s", claim.job_id, exc)
                self._handle_policy_failure(db, claim, str(exc))
            except ScanInterrupted as exc:
                self._handle_interruption(db, claim, str(exc))
            except Exception as exc:
                self.logger.exception("Scan worker failed for job %s", claim.job_id)
                self._handle_failure(db, claim, exc)

    def scanner_status(self, db: Session) -> dict[str, object]:
        control = self.ensure_control(db)
        health = self.scanner.health()
        now = datetime.utcnow()
        active = list(
            db.execute(
                select(Job, ScanRun)
                .join(ScanRun, ScanRun.job_id == Job.id)
                .where(
                    Job.state == "scanning",
                    ScanRun.lease_expires_at.is_not(None),
                    ScanRun.lease_expires_at > now,
                )
            )
        )
        queued = db.scalar(select(func.count()).select_from(Job).where(Job.state == "scan_pending")) or 0
        paused = db.scalar(select(func.count()).select_from(Job).where(Job.state == "scan_paused")) or 0
        return {
            "default_slots": self._default_slots(),
            "requested_slots": control.requested_slots,
            "hard_max_slots": self._hard_max_slots(),
            "max_large_scans": self._max_large_scans(),
            "large_scan_gib": self.settings.large_scan_gib,
            "active": len(active),
            "active_large": sum(1 for job, run in active if self._is_large(job, run)),
            "queued": queued,
            "paused": paused,
            "boost_until_queue_empty": control.boost_until_queue_empty,
            "maintenance_mode": control.maintenance_mode,
            "maintenance_reason": control.maintenance_reason,
            "maintenance_started_at": control.maintenance_started_at,
            "maintenance_drained": control.maintenance_mode and not active,
            "health": health.as_dict(),
        }

    def set_slots(self, db: Session, slots: int) -> dict[str, object]:
        if slots < 1 or slots > self._hard_max_slots():
            raise ValueError(f"scanner slots must be between 1 and {self._hard_max_slots()}")
        control = self.ensure_control(db)
        control.requested_slots = slots
        control.boost_until_queue_empty = slots > self._default_slots()
        control.updated_at = datetime.utcnow()
        db.add(control)
        db.commit()
        return self.scanner_status(db)

    def set_maintenance(
        self,
        db: Session,
        enabled: bool,
        reason: str | None = None,
    ) -> dict[str, object]:
        control = self.ensure_control(db)
        control.maintenance_mode = enabled
        control.maintenance_reason = (reason or "").strip() or None if enabled else None
        control.maintenance_started_at = datetime.utcnow() if enabled else None
        control.updated_at = datetime.utcnow()
        db.add(control)
        db.commit()
        if not enabled:
            self.scanner.clear_health_cache()
        return self.scanner_status(db)

    def prioritize_jobs(self, db: Session, job_ids: list[str]) -> dict[str, object]:
        return self._bulk_apply(job_ids, lambda job_id: self._prioritize_job(db, job_id))

    def pause_jobs(self, db: Session, job_ids: list[str]) -> dict[str, object]:
        return self._bulk_apply(job_ids, lambda job_id: self._pause_job(db, job_id))

    def resume_jobs(self, db: Session, job_ids: list[str]) -> dict[str, object]:
        return self._bulk_apply(job_ids, lambda job_id: self._resume_job(db, job_id))

    def delete_scan_data(self, db: Session, job_id: str) -> None:
        db.execute(delete(ScanFile).where(ScanFile.job_id == job_id))
        db.execute(delete(ScanRun).where(ScanRun.job_id == job_id))

    def enrich_jobs(self, db: Session, jobs: list[Job]) -> list[Job]:
        if not jobs:
            return jobs
        runs = {
            run.job_id: run
            for run in db.scalars(select(ScanRun).where(ScanRun.job_id.in_([job.id for job in jobs])))
        }
        queued = [
            job for job in jobs
            if job.state == "scan_pending" and job.id in runs
        ]
        queued.sort(key=lambda job: self._queue_sort_key(job, runs[job.id], datetime.utcnow()))
        positions = {job.id: index + 1 for index, job in enumerate(queued)}

        for job in jobs:
            run = runs.get(job.id)
            job.scan_priority = run.priority if run else 0
            job.scan_pause_requested = run.pause_requested if run else False
            job.scan_total_files = run.total_files if run else 0
            job.scan_completed_files = run.completed_files if run else 0
            job.scan_total_bytes = run.total_bytes if run else 0
            job.scan_completed_bytes = run.completed_bytes if run else 0
            job.scan_current_file = run.current_file if run else None
            job.scan_current_file_started_at = run.current_file_started_at if run else None
            job.scan_queue_position = positions.get(job.id)
            job.scan_attempts = run.attempts if run else 0
            job.scan_last_error = run.last_error if run else None
            job.scan_progress_percent = self._progress_percent(run)
            job.scan_eta_seconds = None
            job.scan_eta_confidence = None
            if run and job.state == "scanning":
                job.scan_eta_seconds, job.scan_eta_confidence = self._estimate_eta(db, run)
            job.scan_engine_version = run.engine_version if run else None
            job.scan_database_version = run.database_version if run else None
            job.scan_database_updated_at = run.database_updated_at if run else None
            job.scan_policy_version = run.policy_version if run else None
            job.scan_is_large = self._is_large(job, run) if run else (
                isinstance(job.size_bytes, int) and job.size_bytes >= self.settings.large_scan_bytes
            )
            if job.state in SCAN_QUEUE_STATES or job.state in SCAN_ACTION_STATES:
                job.activity_summary = self._scan_activity_summary(job, run)
        return jobs

    def _prepare_manifest(
        self,
        db: Session,
        job: Job,
        run: ScanRun,
        content_path: str,
        identity: ScannerIdentity,
        *,
        heartbeat: Callable[[], bool] | None = None,
    ) -> None:
        root = Path(content_path).resolve()
        manifest = self._filesystem_manifest(root, heartbeat=heartbeat)
        if heartbeat and not heartbeat():
            raise ScanInterrupted("scan lease was lost while preparing the manifest")
        oversized = next(
            (
                (relative_path, size_bytes)
                for relative_path, size_bytes, *_ in manifest
                if size_bytes > self.settings.scanner_max_file_bytes
            ),
            None,
        )
        if oversized:
            relative_path, size_bytes = oversized
            raise ScannerPolicyError(
                f"{relative_path} is {size_bytes} bytes, above the configured ClamAV safety "
                f"limit of {self.settings.scanner_max_file_bytes} bytes. The torrent was not "
                "marked clean because ClamAV cannot fully inspect that file."
            )
        root_changed = run.root_path != str(root)
        legacy_engine = parse_scanner_version(run.scanner_version or "")[0]
        stored_engine = run.engine_version or legacy_engine
        version_changed = bool(
            run.root_path
            and (
                stored_engine != identity.engine_version
                or run.policy_version != identity.policy_version
            )
        )
        if root_changed:
            db.execute(delete(ScanFile).where(ScanFile.job_id == job.id))
            db.flush()

        existing = {
            item.relative_path: item
            for item in db.scalars(select(ScanFile).where(ScanFile.job_id == job.id))
        }
        current_paths = {relative_path for relative_path, *_ in manifest}
        for relative_path, item in existing.items():
            if relative_path not in current_paths:
                db.delete(item)

        for relative_path, size_bytes, mtime_ns, ctime_ns, device, inode in manifest:
            item = existing.get(relative_path)
            if item is None:
                item = ScanFile(
                    job_id=job.id,
                    relative_path=relative_path,
                    size_bytes=size_bytes,
                    mtime_ns=mtime_ns,
                    ctime_ns=ctime_ns,
                    device=device,
                    inode=inode,
                    status="pending",
                )
                db.add(item)
                continue
            if (
                item.size_bytes != size_bytes
                or item.mtime_ns != mtime_ns
                or item.ctime_ns != ctime_ns
                or item.device != device
                or item.inode != inode
                or version_changed
                or item.status not in {"pending", "clean"}
            ):
                item.size_bytes = size_bytes
                item.mtime_ns = mtime_ns
                item.ctime_ns = ctime_ns
                item.device = device
                item.inode = inode
                item.status = "pending"
                item.threat_name = None
                item.scanned_at = None
                item.scanner_version = None
                item.engine_version = None
                item.database_version = None
                item.database_updated_at = None
                item.policy_version = None
                item.scan_started_at = None
                item.scan_duration_seconds = None
                item.last_error = None
                db.add(item)

        db.flush()
        files = list(db.scalars(select(ScanFile).where(ScanFile.job_id == job.id)))
        run.root_path = str(root)
        run.total_files = len(files)
        run.completed_files = sum(1 for item in files if item.status == "clean")
        run.total_bytes = sum(item.size_bytes for item in files)
        run.completed_bytes = sum(item.size_bytes for item in files if item.status == "clean")
        self._sync_run_identity(run, identity)
        run.current_file = None
        run.current_file_started_at = None
        if version_changed:
            run.verdict = None
        run.updated_at = datetime.utcnow()
        db.add(run)
        db.commit()

    def _filesystem_manifest(
        self,
        root: Path,
        *,
        heartbeat: Callable[[], bool] | None = None,
    ) -> list[tuple[str, int, int, int, int, int]]:
        if not root.exists():
            raise RuntimeError(f"scan path does not exist: {root}")
        if root.is_symlink():
            raise RuntimeError(f"scan path cannot be a symbolic link: {root}")
        if root.is_file():
            stat = root.stat()
            return [(".", stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_dev, stat.st_ino)]
        if not root.is_dir():
            raise RuntimeError(f"scan path is not a regular file or directory: {root}")

        manifest: list[tuple[str, int, int, int, int, int]] = []
        files_seen = 0
        for current_root, directories, filenames in os.walk(root):
            if heartbeat and not heartbeat():
                raise ScanInterrupted("scan lease was lost while preparing the manifest")
            current = Path(current_root)
            for directory_name in directories:
                candidate_directory = current / directory_name
                if candidate_directory.is_symlink():
                    raise RuntimeError(f"scan manifest contains a symbolic-link directory: {candidate_directory}")
            directories[:] = sorted(directories)
            for filename in sorted(filenames):
                candidate = current / filename
                if candidate.is_symlink():
                    raise RuntimeError(f"scan manifest contains a symbolic-link file: {candidate}")
                if not candidate.is_file():
                    raise RuntimeError(f"scan manifest contains a non-regular file: {candidate}")
                resolved = candidate.resolve()
                if root not in resolved.parents:
                    raise RuntimeError(f"scan file escaped its root: {candidate}")
                stat = resolved.stat()
                manifest.append(
                    (
                        resolved.relative_to(root).as_posix(),
                        stat.st_size,
                        stat.st_mtime_ns,
                        stat.st_ctime_ns,
                        stat.st_dev,
                        stat.st_ino,
                    )
                )
                files_seen += 1
                if files_seen % 256 == 0 and heartbeat and not heartbeat():
                    raise ScanInterrupted("scan lease was lost while preparing the manifest")
        return manifest

    def _refresh_file_fingerprint(self, scan_file: ScanFile, absolute_path: Path) -> None:
        if not absolute_path.exists() or not absolute_path.is_file() or absolute_path.is_symlink():
            raise RuntimeError(f"scan file is unavailable or unsafe: {absolute_path}")
        stat = absolute_path.stat()
        if (
            stat.st_size != scan_file.size_bytes
            or stat.st_mtime_ns != scan_file.mtime_ns
            or stat.st_ctime_ns != scan_file.ctime_ns
            or stat.st_dev != scan_file.device
            or stat.st_ino != scan_file.inode
        ):
            scan_file.size_bytes = stat.st_size
            scan_file.mtime_ns = stat.st_mtime_ns
            scan_file.ctime_ns = stat.st_ctime_ns
            scan_file.device = stat.st_dev
            scan_file.inode = stat.st_ino
            scan_file.status = "pending"
            scan_file.scanned_at = None
            scan_file.scanner_version = None
            scan_file.engine_version = None
            scan_file.database_version = None
            scan_file.database_updated_at = None
            scan_file.policy_version = None
            scan_file.scan_started_at = None
            scan_file.scan_duration_seconds = None

    def _finish_clean_scan(self, db: Session, job: Job, run: ScanRun) -> None:
        run.verdict = "clean"
        run.current_file = None
        run.current_file_started_at = None
        run.last_error = None
        run.updated_at = datetime.utcnow()
        job.scan_completed_at = datetime.utcnow()
        job.state = "scan_clean"
        job.updated_at = datetime.utcnow()
        job.last_error = None
        self._release_lease(run)
        db.add_all([job, run])
        db.commit()

    def _heartbeat(self, claim: ScanClaim, stop_event: threading.Event) -> bool:
        if stop_event.is_set():
            return False
        with SessionLocal() as db:
            job_exists = db.scalar(select(func.count()).select_from(Job).where(Job.id == claim.job_id))
            if not job_exists:
                return False
            now = datetime.utcnow()
            renewed = db.execute(
                update(ScanRun)
                .where(ScanRun.job_id == claim.job_id, ScanRun.worker_id == claim.worker_id)
                .values(
                    heartbeat_at=now,
                    lease_expires_at=now + timedelta(seconds=self._lease_seconds()),
                    updated_at=now,
                )
            )
            db.commit()
            return renewed.rowcount == 1

    def _handle_interruption(self, db: Session, claim: ScanClaim, message: str) -> None:
        db.rollback()
        job = db.get(Job, claim.job_id)
        run = db.get(ScanRun, claim.job_id)
        if not job or not run or run.worker_id != claim.worker_id:
            return
        db.execute(
            update(ScanFile)
            .where(ScanFile.job_id == job.id, ScanFile.status == "scanning")
            .values(
                status="pending",
                scan_started_at=None,
                scan_duration_seconds=None,
                last_error=message,
            )
        )
        job.state = "scan_paused" if run.pause_requested else "scan_pending"
        job.updated_at = datetime.utcnow()
        run.queued_at = datetime.utcnow()
        run.last_error = message
        run.current_file = None
        run.current_file_started_at = None
        run.updated_at = datetime.utcnow()
        self._release_lease(run)
        db.add_all([job, run])
        db.commit()

    def _handle_failure(self, db: Session, claim: ScanClaim, exc: Exception) -> None:
        db.rollback()
        job = db.get(Job, claim.job_id)
        run = db.get(ScanRun, claim.job_id)
        if not job or not run or run.worker_id != claim.worker_id:
            return
        message = str(exc).strip() or repr(exc)
        db.execute(
            update(ScanFile)
            .where(ScanFile.job_id == job.id, ScanFile.status == "scanning")
            .values(
                status="pending",
                scan_started_at=None,
                scan_duration_seconds=None,
                last_error=message,
            )
        )
        run.failure_count += 1
        run.last_error = message
        run.current_file = None
        run.current_file_started_at = None
        run.updated_at = datetime.utcnow()
        self._release_lease(run)

        if run.failure_count >= max(self.settings.scan_max_failures, 1):
            job.state = "error"
            job.is_terminal = True
            job.last_error = f"Scanner failed after {run.failure_count} attempts: {message}"
        else:
            delay = max(self.settings.scan_retry_base_seconds, 1) * (2 ** (run.failure_count - 1))
            run.next_attempt_at = datetime.utcnow() + timedelta(seconds=delay)
            run.queued_at = datetime.utcnow()
            job.state = "scan_pending"
            job.is_terminal = False
            job.last_error = f"Scan interrupted; retrying in {delay}s: {message}"
        job.updated_at = datetime.utcnow()
        db.add_all([job, run])
        db.commit()
        self._emit_scan_failure(job, message)

    def _defer_claim(
        self,
        db: Session,
        claim: ScanClaim,
        message: str,
        *,
        retry_after_seconds: int = 0,
    ) -> None:
        db.rollback()
        job = db.get(Job, claim.job_id)
        run = db.get(ScanRun, claim.job_id)
        if not job or not run or run.worker_id != claim.worker_id:
            return
        db.execute(
            update(ScanFile)
            .where(ScanFile.job_id == job.id, ScanFile.status == "scanning")
            .values(
                status="pending",
                scan_started_at=None,
                scan_duration_seconds=None,
                last_error=message,
            )
        )
        now = datetime.utcnow()
        job.state = "scan_paused" if run.pause_requested else "scan_pending"
        job.is_terminal = False
        job.last_error = message
        job.updated_at = now
        run.queued_at = now
        run.next_attempt_at = (
            now + timedelta(seconds=max(retry_after_seconds, 0))
            if retry_after_seconds
            else None
        )
        run.last_error = message
        run.current_file = None
        run.current_file_started_at = None
        run.updated_at = now
        self._release_lease(run)
        db.add_all([job, run])
        db.commit()
        self._emit_scan_failure(job, message)

    def _handle_policy_failure(self, db: Session, claim: ScanClaim, message: str) -> None:
        db.rollback()
        job = db.get(Job, claim.job_id)
        run = db.get(ScanRun, claim.job_id)
        if not job or not run or run.worker_id != claim.worker_id:
            return
        db.execute(
            update(ScanFile)
            .where(ScanFile.job_id == job.id, ScanFile.status == "scanning")
            .values(
                status="error",
                scan_duration_seconds=None,
                last_error=message,
            )
        )
        now = datetime.utcnow()
        job.state = "error"
        job.is_terminal = True
        job.last_error = f"Scanner safety check blocked promotion: {message}"
        job.updated_at = now
        run.verdict = "error"
        run.last_error = message
        run.current_file = None
        run.current_file_started_at = None
        run.next_attempt_at = None
        run.updated_at = now
        self._release_lease(run)
        db.add_all([job, run])
        db.commit()
        self._emit_scan_failure(job, message)

    def _should_yield(self, db: Session, job: Job, run: ScanRun, is_large: bool) -> bool:
        queued = list(
            db.execute(
                select(Job, ScanRun)
                .join(ScanRun, ScanRun.job_id == Job.id)
                .where(
                    Job.state == "scan_pending",
                    ScanRun.pause_requested == False,
                    or_(ScanRun.next_attempt_at.is_(None), ScanRun.next_attempt_at <= datetime.utcnow()),
                )
            )
        )
        if any(other_run.priority > run.priority for _, other_run in queued):
            return True
        if is_large and any(not self._is_large(other_job, other_run) for other_job, other_run in queued):
            return True
        return False

    def _prioritize_job(self, db: Session, job_id: str) -> None:
        job = db.get(Job, job_id)
        if not job:
            raise LookupError("Job not found")
        if job.state not in {"scan_pending", "scanning"}:
            raise ValueError("Only queued or active scans can be prioritized; resume paused scans explicitly")
        run = db.get(ScanRun, job.id)
        if run is None:
            run = ScanRun(job_id=job.id)
        run.priority = max(run.priority or 0, 100)
        run.queued_at = datetime.utcnow()
        run.updated_at = datetime.utcnow()
        db.add_all([job, run])
        db.commit()

    def _pause_job(self, db: Session, job_id: str) -> None:
        job = db.get(Job, job_id)
        run = db.get(ScanRun, job_id)
        if not job or not run:
            raise LookupError("Scan job not found")
        if job.state not in {"scan_pending", "scanning"}:
            raise ValueError("Only queued or active scans can be paused")
        run.pause_requested = True
        run.updated_at = datetime.utcnow()
        if job.state == "scan_pending":
            job.state = "scan_paused"
            job.updated_at = datetime.utcnow()
        db.add_all([job, run])
        db.commit()

    def _resume_job(self, db: Session, job_id: str) -> None:
        job = db.get(Job, job_id)
        run = db.get(ScanRun, job_id)
        if not job or not run:
            raise LookupError("Scan job not found")
        if job.state != "scan_paused":
            raise ValueError("Only paused scans can be resumed")
        run.pause_requested = False
        run.queued_at = datetime.utcnow()
        run.next_attempt_at = None
        run.updated_at = datetime.utcnow()
        job.state = "scan_pending"
        job.is_terminal = False
        job.updated_at = datetime.utcnow()
        db.add_all([job, run])
        db.commit()

    def _bulk_apply(self, job_ids: list[str], operation) -> dict[str, object]:
        unique_ids = list(dict.fromkeys(job_ids))
        result: dict[str, object] = {
            "requested": len(unique_ids),
            "processed": 0,
            "skipped": 0,
            "failed": 0,
            "processed_ids": [],
            "skipped_ids": [],
            "failed_ids": [],
            "errors": {},
        }
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

    def _find_torrent(self, job: Job):
        torrent = self.qbt.get_torrent(job.qbt_hash) if job.qbt_hash else None
        if torrent is None and job.unique_tag:
            torrent = self.qbt.find_by_unique_tag(job.unique_tag)
        return torrent

    def _wait_for_paused_torrent(
        self,
        db: Session,
        job: Job,
        claim: ScanClaim,
        stop_event: threading.Event,
    ):
        deadline = time.monotonic() + max(self.settings.pause_confirmation_timeout_seconds, 1)
        while time.monotonic() < deadline:
            if stop_event.is_set() or not self._heartbeat(claim, stop_event):
                raise ScanInterrupted("scan stopped while waiting for qBittorrent to pause")
            torrent = self._find_torrent(job)
            if torrent is None:
                raise RuntimeError("qBittorrent torrent disappeared while pausing")
            self.guard.validate_common(db, job, torrent, require_paused=False)
            if self.guard.is_paused(torrent):
                return torrent
            time.sleep(0.5)
        raise RuntimeError("qBittorrent did not confirm the torrent was paused before scanning")

    def _emit_scan_failure(self, job: Job, message: str) -> None:
        try:
            emit_event(
                "scan_failed",
                "warning",
                message[:2000],
                source_path=job.content_path,
                action_success=False,
                job_id=job.id,
                torrent_hash=job.qbt_hash,
            )
        except Exception:
            self.logger.exception("Could not persist scan_failed event for job %s", job.id)

    def _absolute_scan_path(self, root_path: str | None, relative_path: str) -> Path:
        if not root_path:
            raise RuntimeError("scan manifest has no root path")
        root = Path(root_path).resolve()
        candidate = root if relative_path == "." else (root / relative_path).resolve()
        if candidate != root and root not in candidate.parents:
            raise RuntimeError(f"scan file escaped its root: {relative_path}")
        return candidate

    def _maintenance_requested(self, db: Session) -> bool:
        return bool(
            db.scalar(
                select(ScannerControl.maintenance_mode).where(ScannerControl.id == 1)
            )
        )

    def _identity_requires_reset(self, run: ScanRun, identity: ScannerIdentity) -> bool:
        if not run.root_path:
            return False
        legacy_engine = parse_scanner_version(run.scanner_version or "")[0]
        stored_engine = run.engine_version or legacy_engine
        return (
            stored_engine != identity.engine_version
            or run.policy_version != identity.policy_version
        )

    @staticmethod
    def _sync_run_identity(run: ScanRun, identity: ScannerIdentity) -> None:
        run.scanner_version = identity.raw_version
        run.engine_version = identity.engine_version
        run.database_version = identity.database_version
        run.database_updated_at = identity.database_updated_at
        run.policy_version = identity.policy_version

    @staticmethod
    def _progress_percent(run: ScanRun | None) -> float | None:
        if run is None:
            return None
        if run.total_bytes > 0:
            return round(min(max(run.completed_bytes / run.total_bytes * 100, 0.0), 100.0), 1)
        if run.total_files > 0:
            return round(min(max(run.completed_files / run.total_files * 100, 0.0), 100.0), 1)
        if run.verdict == "clean":
            return 100.0
        return None

    def _estimate_eta(self, db: Session, run: ScanRun) -> tuple[int | None, str | None]:
        samples = list(
            db.scalars(
                select(ScanFile)
                .where(
                    ScanFile.job_id == run.job_id,
                    ScanFile.status == "clean",
                    ScanFile.scan_duration_seconds.is_not(None),
                    ScanFile.scan_duration_seconds > 0,
                )
                .order_by(ScanFile.scanned_at.desc())
                .limit(100)
            )
        )
        if len(samples) < 3:
            return None, None

        remaining_count, remaining_bytes = db.execute(
            select(func.count(), func.coalesce(func.sum(ScanFile.size_bytes), 0)).where(
                ScanFile.job_id == run.job_id,
                ScanFile.status.in_(("pending", "scanning")),
            )
        ).one()
        if not remaining_count:
            return 0, "high"

        mib = 1024 * 1024
        sizes = [sample.size_bytes / mib for sample in samples]
        durations = [float(sample.scan_duration_seconds or 0) for sample in samples]
        mean_size = fmean(sizes)
        mean_duration = fmean(durations)
        variance = sum((size - mean_size) ** 2 for size in sizes)
        if variance > 1e-9:
            slope = max(
                sum(
                    (size - mean_size) * (duration - mean_duration)
                    for size, duration in zip(sizes, durations)
                )
                / variance,
                0.0,
            )
            intercept = max(mean_duration - slope * mean_size, 0.05)
        else:
            slope = 0.0
            intercept = max(mean_duration, 0.05)

        estimate = intercept * int(remaining_count) + slope * (int(remaining_bytes) / mib)
        predictions = [intercept + slope * size for size in sizes]
        relative_error = fmean(
            abs(predicted - duration) / max(duration, 0.1)
            for predicted, duration in zip(predictions, durations)
        )

        current = db.scalar(
            select(ScanFile).where(
                ScanFile.job_id == run.job_id,
                ScanFile.status == "scanning",
            )
        )
        if current and current.scan_started_at:
            elapsed = max((datetime.utcnow() - current.scan_started_at).total_seconds(), 0.0)
            predicted_current = intercept + slope * (current.size_bytes / mib)
            if elapsed > max(predicted_current * 2, 60):
                return None, "low"
            estimate -= min(elapsed, predicted_current)

        if len(samples) >= 30 and relative_error <= 0.35:
            confidence = "high"
        elif len(samples) >= 10 and relative_error <= 0.75:
            confidence = "medium"
        else:
            confidence = "low"
        return max(int(estimate + 0.5), 0), confidence

    def _scan_activity_summary(self, job: Job, run: ScanRun | None) -> str:
        state_labels = {
            "scan_pending": "Scan queued",
            "scanning": "Scanning",
            "scan_paused": "Scan paused",
            "scan_clean": "Scan clean; promotion queued",
            "promoting": "Promoting clean torrent",
            "scan_infected": "Threat found; safe action queued",
            "quarantining_infected": "Moving infected torrent to quarantine",
            "deleting_infected": "Deleting infected torrent",
        }
        parts = [state_labels.get(job.state, "Scan pending")]
        if run is None:
            return parts[0]
        if job.state == "scan_pending" and getattr(job, "scan_queue_position", None):
            parts[0] += f" #{job.scan_queue_position}"
        if run.total_files:
            parts.append(f"{run.completed_files}/{run.total_files} files")
        elif job.state in SCAN_QUEUE_STATES:
            parts.append("preparing manifest")
        if run.total_bytes:
            parts.append(
                f"{self._format_bytes(run.completed_bytes)}/{self._format_bytes(run.total_bytes)}"
            )
        if run.current_file:
            current_file = run.current_file
            if len(current_file) > 90:
                current_file = f"...{current_file[-87:]}"
            parts.append(current_file)
        if run.pause_requested and job.state == "scanning":
            parts.append("pause requested after current file")
        if self._is_large(job, run):
            parts.append("large-scan slot")
        return " | ".join(parts)

    @staticmethod
    def _format_bytes(value: int) -> str:
        units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
        size = float(max(value, 0))
        unit_index = 0
        while size >= 1024 and unit_index < len(units) - 1:
            size /= 1024
            unit_index += 1
        return f"{int(size)} {units[unit_index]}" if unit_index == 0 else f"{size:.2f} {units[unit_index]}"

    def _queue_sort_key(self, job: Job, run: ScanRun, now: datetime) -> tuple[int, bool, int, datetime]:
        queued_at = run.queued_at or job.updated_at or job.created_at
        age_hours = max(int((now - queued_at).total_seconds() // 3600), 0)
        effective_priority = run.priority + min(age_hours, 50)
        size = run.total_bytes or job.size_bytes or 0
        return (-effective_priority, self._is_large(job, run), size, queued_at)

    def _is_large(self, job: Job, run: ScanRun | None) -> bool:
        size = (run.total_bytes if run else 0) or job.size_bytes or 0
        return size >= self.settings.large_scan_bytes

    def _release_lease(self, run: ScanRun) -> None:
        run.worker_id = None
        run.lease_expires_at = None
        run.heartbeat_at = None

    def _default_slots(self) -> int:
        return min(max(self.settings.max_concurrent_scans, 1), self._hard_max_slots())

    def _hard_max_slots(self) -> int:
        return max(self.settings.max_scan_slots, 1)

    def _max_large_scans(self) -> int:
        return min(max(self.settings.max_concurrent_large_scans, 1), self._hard_max_slots())

    def _lease_seconds(self) -> int:
        return max(self.settings.scan_lease_seconds, self.settings.scan_heartbeat_seconds * 3, 30)
