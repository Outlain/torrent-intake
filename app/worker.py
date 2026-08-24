from __future__ import annotations

import asyncio
import logging
import socket
import threading
from uuid import uuid4

from sqlalchemy import select

from .config import get_settings
from .db import SessionLocal
from .models import Job
from .scan_coordinator import ScanClaim
from .service import JobService

logger = logging.getLogger(__name__)
QBT_HASH_RETRY_INTERVAL_SECONDS = 5


def _run_management_cycle(service: JobService, startup_diagnostics_logged: bool) -> bool:
    with SessionLocal() as db:
        if not startup_diagnostics_logged:
            try:
                service.log_local_staging_diagnostics(db)
            except Exception:
                logger.exception("Startup local staging diagnostics failed")
            else:
                startup_diagnostics_logged = True
        service.process_nonterminal_jobs(db)
    return startup_diagnostics_logged


def _run_qbt_hash_retry_cycle(service: JobService) -> int:
    """Retry only jobs waiting for qBittorrent to expose their canonical hash.

    qBittorrent can accept a magnet before the new torrent/tag is visible through
    torrents/info. Keep this fast path separate from the general management poll
    so a short propagation race does not leave a healthy torrent waiting for up
    to several minutes.
    """
    with SessionLocal() as db:
        jobs = list(
            db.scalars(
                select(Job)
                .where(Job.is_terminal == False)
                .where(Job.state == "waiting_for_qbt_hash")
                .order_by(Job.created_at.asc())
            )
        )
        for job in jobs:
            try:
                service._resolve_hash_for_job(db, job)
            except Exception:
                db.rollback()
                logger.exception(
                    "Fast qB hash resolution failed for job %s; keeping it queued for retry",
                    job.id,
                )
        return len(jobs)


def _recover_scan_state(service: JobService) -> None:
    with SessionLocal() as db:
        # No scan process survives an application restart, so every old lease is stale.
        service.scan_coordinator.recover_interrupted_scans(db, force=True)
        service.process_scan_actions(db)


def _run_scan_scheduler_cycle(service: JobService, scheduler_id: str) -> list[ScanClaim]:
    with SessionLocal() as db:
        service.process_scan_actions(db)
        return service.scan_coordinator.claim_jobs(db, scheduler_id)


def _reap_scan_tasks(tasks: dict[str, asyncio.Task[None]]) -> None:
    for job_id, task in list(tasks.items()):
        if not task.done():
            continue
        tasks.pop(job_id, None)
        try:
            task.result()
        except asyncio.CancelledError:
            logger.info("Scan task cancelled for job %s", job_id)
        except Exception:
            logger.exception("Unhandled scan task failure for job %s", job_id)


async def worker_loop(stop_event: asyncio.Event) -> None:
    settings = get_settings()
    service = JobService()
    scanner_stop_event = threading.Event()
    scheduler_id = f"{socket.gethostname()}:{uuid4().hex[:12]}"
    scan_tasks: dict[str, asyncio.Task[None]] = {}
    startup_diagnostics_logged = False
    loop = asyncio.get_running_loop()
    next_management_cycle = 0.0
    next_qbt_hash_retry_cycle = 0.0

    try:
        try:
            await asyncio.to_thread(_recover_scan_state, service)
        except Exception:
            logger.exception("Scan restart recovery failed")

        while not stop_event.is_set():
            _reap_scan_tasks(scan_tasks)
            now = loop.time()
            if now >= next_management_cycle:
                try:
                    startup_diagnostics_logged = await asyncio.to_thread(
                        _run_management_cycle,
                        service,
                        startup_diagnostics_logged,
                    )
                except Exception:
                    logger.exception("Background management cycle failed")
                next_management_cycle = loop.time() + max(settings.polling_interval_seconds, 1)

            if now >= next_qbt_hash_retry_cycle:
                try:
                    await asyncio.to_thread(_run_qbt_hash_retry_cycle, service)
                except Exception:
                    logger.exception("Background qB hash retry cycle failed")
                next_qbt_hash_retry_cycle = loop.time() + QBT_HASH_RETRY_INTERVAL_SECONDS

            try:
                claims = await asyncio.to_thread(_run_scan_scheduler_cycle, service, scheduler_id)
            except Exception:
                logger.exception("Scan scheduler cycle failed")
                claims = []

            for claim in claims:
                existing = scan_tasks.get(claim.job_id)
                if existing is not None and not existing.done():
                    logger.error("Refusing duplicate local scan task for job %s", claim.job_id)
                    continue
                scan_tasks[claim.job_id] = asyncio.create_task(
                    asyncio.to_thread(service.scan_coordinator.run_claim, claim, scanner_stop_event)
                )

            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=max(settings.scan_scheduler_interval_seconds, 1),
                )
            except asyncio.TimeoutError:
                pass
    finally:
        scanner_stop_event.set()
        if scan_tasks:
            results = await asyncio.gather(*scan_tasks.values(), return_exceptions=True)
            for job_id, result in zip(scan_tasks, results):
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    logger.error(
                        "Scan task failed while shutting down for job %s: %r",
                        job_id,
                        result,
                    )
