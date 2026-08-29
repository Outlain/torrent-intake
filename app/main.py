from __future__ import annotations
import asyncio
import hmac
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from .config import get_settings
from .db import Base, engine, get_db, upgrade_schema
from .models import Job
from .schemas import (
    CompletionEventIn,
    JobBatchCreate,
    JobBatchCreateResult,
    JobBulkResult,
    JobCreate,
    JobOut,
    JobSelectionIn,
    ScannerMaintenanceUpdate,
    ScannerSlotsUpdate,
)
from .service import JobService
from .settings_view import build_settings_catalog
from .tags import MAX_CUSTOM_TAG_LENGTH, MAX_CUSTOM_TAGS, PRIVATE_JOB_TAG_PREFIX
from .worker import worker_loop

logging.basicConfig(
    level=logging.DEBUG if get_settings().debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

settings = get_settings()
service = JobService()
BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))
worker_stop_event: asyncio.Event | None = None
worker_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global worker_stop_event, worker_task
    Base.metadata.create_all(bind=engine)
    upgrade_schema()
    worker_stop_event = asyncio.Event()
    worker_task = asyncio.create_task(worker_loop(worker_stop_event))
    yield
    if worker_stop_event:
        worker_stop_event.set()
    if worker_task:
        await worker_task


app = FastAPI(title=settings.ui_title, lifespan=lifespan)


def _enrich_jobs(db: Session, jobs: list[Job]) -> list[Job]:
    service.enrich_jobs_with_live_stats(jobs)
    return service.scan_coordinator.enrich_jobs(db, jobs)


def _validate_completion_event_token(token: str | None) -> None:
    expected = settings.completion_event_token
    if not expected:
        raise HTTPException(status_code=503, detail="Completion event authentication is not configured")
    if token is None or not hmac.compare_digest(token, expected):
        logger.warning("Rejected qB completion event due to invalid token")
        raise HTTPException(status_code=403, detail="Invalid completion event token")


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/ui", status_code=307)


@app.get("/health")
def health() -> dict[str, str]:
    scanner = service.scan_coordinator.scanner.health()
    data_dir = Path("/app/data")
    event_dir = Path(settings.event_dir)
    if not data_dir.is_dir() or not os.access(data_dir, os.R_OK | os.W_OK | os.X_OK):
        raise HTTPException(status_code=503, detail="persistent data directory is unavailable")
    if not event_dir.is_dir() or not os.access(event_dir, os.R_OK | os.W_OK | os.X_OK):
        raise HTTPException(status_code=503, detail="event directory is unavailable")
    if not scanner.can_scan:
        raise HTTPException(status_code=503, detail=scanner.message)
    return {"status": "ok"}


@app.get("/jobs", response_model=list[JobOut])
def list_jobs(db: Session = Depends(get_db)):
    jobs = list(db.scalars(select(Job).order_by(Job.created_at.desc())))
    return _enrich_jobs(db, jobs)


@app.post("/jobs", response_model=JobOut)
def create_job(payload: JobCreate, db: Session = Depends(get_db)):
    try:
        job = service.submit_job(
            db,
            magnet_uri=payload.magnet_uri,
            final_parent=payload.final_parent,
            final_category=payload.final_category,
            staging_preference=payload.staging_preference,
            custom_tags=payload.custom_tags,
        )
        return job
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/jobs/bulk", response_model=JobBatchCreateResult)
def create_jobs_bulk(payload: JobBatchCreate, db: Session = Depends(get_db)):
    result: dict[str, object] = {
        "requested": len(payload.jobs),
        "created": 0,
        "failed": 0,
        "jobs": [],
        "errors": {},
    }
    created_jobs: list[Job] = []
    errors: dict[str, str] = {}

    for index, item in enumerate(payload.jobs, start=1):
        try:
            job = service.submit_job(
                db,
                magnet_uri=item.magnet_uri,
                final_parent=item.final_parent,
                final_category=item.final_category,
                staging_preference=item.staging_preference,
                custom_tags=item.custom_tags,
            )
            created_jobs.append(job)
        except (ValueError, RuntimeError) as exc:
            errors[str(index)] = str(exc)

    result["created"] = len(created_jobs)
    result["failed"] = len(errors)
    result["jobs"] = created_jobs
    result["errors"] = errors
    return result


@app.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return _enrich_jobs(db, [job])[0]


@app.post("/jobs/{job_id}/retry", response_model=JobOut)
def retry_job(job_id: str, db: Session = Depends(get_db)):
    try:
        return service.retry_job(db, job_id=job_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/jobs/bulk-retry", response_model=JobBulkResult)
def bulk_retry_jobs(payload: JobSelectionIn, db: Session = Depends(get_db)):
    return service.retry_jobs(db, job_ids=payload.job_ids)


@app.post("/jobs/bulk-scan-next", response_model=JobBulkResult)
def bulk_prioritize_scans(payload: JobSelectionIn, db: Session = Depends(get_db)):
    return service.scan_coordinator.prioritize_jobs(db, payload.job_ids)


@app.post("/jobs/bulk-scan-pause", response_model=JobBulkResult)
def bulk_pause_scans(payload: JobSelectionIn, db: Session = Depends(get_db)):
    return service.scan_coordinator.pause_jobs(db, payload.job_ids)


@app.post("/jobs/bulk-scan-resume", response_model=JobBulkResult)
def bulk_resume_scans(payload: JobSelectionIn, db: Session = Depends(get_db)):
    return service.scan_coordinator.resume_jobs(db, payload.job_ids)


@app.post("/jobs/bulk-move-to-nas", response_model=JobBulkResult)
def bulk_move_waiting_jobs_to_nas(payload: JobSelectionIn, db: Session = Depends(get_db)):
    return service.move_waiting_jobs_to_nas(db, job_ids=payload.job_ids)


@app.get("/qbt/categories")
def qbt_categories():
    try:
        return {"categories": service.qbt.list_categories()}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch qBittorrent categories: {exc}") from exc


@app.get("/qbt/tags")
def qbt_tags(db: Session = Depends(get_db)):
    try:
        return {"tags": service.list_selectable_qbt_tags(db)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch qBittorrent tags: {exc}") from exc


@app.get("/qbt/final-path-suggestions")
def qbt_final_path_suggestions():
    try:
        return {"paths": service.qbt.list_save_path_suggestions()}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch qBittorrent path suggestions: {exc}") from exc


@app.get("/qbt/transfer")
def qbt_transfer_info():
    try:
        return service.qbt.transfer_info()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch qBittorrent transfer info: {exc}") from exc


@app.get("/scanner/status")
def scanner_status(db: Session = Depends(get_db)):
    return service.scan_coordinator.scanner_status(db)


@app.post("/scanner/slots")
def update_scanner_slots(payload: ScannerSlotsUpdate, db: Session = Depends(get_db)):
    try:
        return service.scan_coordinator.set_slots(db, payload.slots)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/scanner/maintenance")
def update_scanner_maintenance(
    payload: ScannerMaintenanceUpdate,
    db: Session = Depends(get_db),
):
    return service.scan_coordinator.set_maintenance(
        db,
        enabled=payload.enabled,
        reason=payload.reason,
    )


@app.get("/fs/final-path-suggestions")
def fs_final_path_suggestions(prefix: str | None = Query(default=None)):
    try:
        return {"paths": service.suggest_final_paths(prefix)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch filesystem path suggestions: {exc}") from exc


@app.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job(job_id: str, db: Session = Depends(get_db)):
    try:
        service.delete_job(db, job_id=job_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/jobs/bulk-delete", response_model=JobBulkResult)
def bulk_delete_jobs(payload: JobSelectionIn, db: Session = Depends(get_db)):
    return service.delete_jobs(db, job_ids=payload.job_ids)


@app.post("/jobs/clear-completed", response_model=JobBulkResult)
def clear_completed_jobs(db: Session = Depends(get_db)):
    return service.delete_jobs_by_states(
        db,
        states={"done", "infected_held", "infected_quarantined", "infected_deleted"},
    )


@app.post("/jobs/clear-failed", response_model=JobBulkResult)
def clear_failed_jobs(db: Session = Depends(get_db)):
    return service.delete_jobs_by_states(db, states={"error"})


@app.post("/events/qbt-complete")
def qbt_complete_event(payload: CompletionEventIn, db: Session = Depends(get_db)):
    _validate_completion_event_token(payload.token)
    logger.info(
        "Received qB completion event qbt_hash=%s qbt_hash_v2=%s torrent_name=%s tags=%s",
        payload.qbt_hash,
        payload.qbt_hash_v2,
        payload.torrent_name,
        payload.tags,
    )
    job = service.ingest_completion_event(
        db,
        qbt_hash=payload.qbt_hash,
        qbt_hash_v2=payload.qbt_hash_v2,
        unique_tag=payload.unique_tag,
        tags=payload.tags,
        torrent_name=payload.torrent_name,
        content_path=payload.content_path,
        root_path=payload.root_path,
        save_path=payload.save_path,
        size_bytes=payload.size_bytes,
    )
    if not job:
        raise HTTPException(status_code=404, detail="No matching job found")
    try:
        job = service.process_job_immediately(db, job_id=job.id, ignore_event_grace=True)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"status": "accepted", "job_id": job.id, "state": job.state}


@app.post("/events/qbt-complete-form")
def qbt_complete_event_form(
    qbt_hash: str | None = Form(default=None),
    qbt_hash_v2: str | None = Form(default=None),
    unique_tag: str | None = Form(default=None),
    torrent_name: str | None = Form(default=None),
    content_path: str | None = Form(default=None),
    root_path: str | None = Form(default=None),
    save_path: str | None = Form(default=None),
    category: str | None = Form(default=None),
    tags: str | None = Form(default=None),
    tracker: str | None = Form(default=None),
    size_bytes: int | None = Form(default=None),
    files_count: int | None = Form(default=None),
    torrent_id: str | None = Form(default=None),
    token: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    payload = CompletionEventIn(
        qbt_hash=qbt_hash,
        qbt_hash_v2=qbt_hash_v2,
        unique_tag=unique_tag,
        torrent_name=torrent_name,
        content_path=content_path,
        root_path=root_path,
        save_path=save_path,
        category=category,
        tags=tags,
        tracker=tracker,
        size_bytes=size_bytes,
        files_count=files_count,
        torrent_id=torrent_id,
        token=token,
    )
    return qbt_complete_event(payload, db)


@app.get("/ui", response_class=HTMLResponse)
def ui(request: Request, db: Session = Depends(get_db)):
    jobs = list(db.scalars(select(Job).order_by(Job.created_at.desc()).limit(50)))
    jobs = _enrich_jobs(db, jobs)
    return TEMPLATES.TemplateResponse(
        request,
        "index.html",
        {
            "title": settings.ui_title,
            "jobs": jobs,
            "settings": settings,
            "settings_catalog": build_settings_catalog(settings),
            "max_custom_tags": MAX_CUSTOM_TAGS,
            "max_custom_tag_length": MAX_CUSTOM_TAG_LENGTH,
            "private_job_tag_prefix": PRIVATE_JOB_TAG_PREFIX,
        },
    )
