from __future__ import annotations
import asyncio
import base64
import hmac
import json
import logging
import os
import tempfile
import sqlite3
import zipfile
from urllib.parse import urlsplit
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask
from cryptography.exceptions import InvalidTag
from .admin import Controller
from .backup import MAX_BACKUP_BYTES, MAX_DATABASE_BYTES, create_backup, database_path, database_size_bytes
from .config import Settings, get_settings, persist_settings
from .restore import stage_restore
from .state_files import write_private
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
from .settings_editor import SettingsEditError, pending_settings, revision, validate_draft
from .qbt import QbtService
from .tags import MAX_CUSTOM_TAG_LENGTH, MAX_CUSTOM_TAGS, PRIVATE_JOB_TAG_PREFIX

logging.basicConfig(
    level=logging.DEBUG if get_settings().debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

settings = get_settings()
service = JobService()
BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))
controller: Controller | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global controller
    persist_settings(settings)
    if (Path(settings.data_dir) / ".restore-pending").exists():
        raise RuntimeError("A restore is pending; start the container through its standard image entrypoint")
    # Opening create_all below creates the database on a fresh installation.
    try:
        fresh = not database_path(settings).exists()
    except ValueError:
        fresh = False  # Non-SQLite deployments use their own database backup tooling.
    Base.metadata.create_all(bind=engine)
    upgrade_schema()
    controller = Controller(settings, fresh=fresh)
    if not controller.paused:
        controller.start()
    try:
        yield
    finally:
        await controller.shutdown()


app = FastAPI(title=settings.ui_title, lifespan=lifespan)


@app.middleware("http")
async def administration_guard(request: Request, call_next):
    is_admin = request.url.path.startswith("/admin/")
    if controller is not None and controller.paused and request.url.path.startswith("/qbt/"):
        return JSONResponse({"detail": "Live qBittorrent lookups are paused with the controller"}, status_code=503)
    if is_admin:
        if controller is None:
            return JSONResponse({"detail": "Controller is starting"}, status_code=503)
        if not controller.authorized(request.headers.get("X-TI-Admin-Token", "")):
            return JSONResponse({"detail": "A valid local administrator token is required"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "Cross-origin administration is not allowed"}, status_code=403)
    mutating = not is_admin and request.method not in {"GET", "HEAD", "OPTIONS"} and controller is not None
    if mutating:
        if controller.paused:
            return JSONResponse({"detail": "Controller is paused for backup, restore, or configuration"}, status_code=503)
        controller.active_mutations += 1
    try:
        response = await call_next(request)
        if is_admin:
            response.headers["Cache-Control"] = "no-store"
        return response
    finally:
        if mutating:
            controller.active_mutations -= 1


async def _admin_json(request: Request) -> dict:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 65536:
            raise HTTPException(status_code=413, detail="Administration request is too large")
        body.extend(chunk)
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError):
        raise HTTPException(status_code=422, detail="Expected a JSON object")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Expected a JSON object")
    return payload


async def _finish_thread(function, *args, **kwargs):
    """Do not release the restore lock/delete work files while a thread still runs."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


@app.get("/controller/status")
def controller_status():
    return controller.status() if controller else {"paused": True, "drained": False}


@app.get("/admin/status")
def admin_status():
    try:
        database_bytes = database_size_bytes(settings)
    except (OSError, ValueError, sqlite3.DatabaseError):
        database_bytes = None
    return {
        **controller.status(), "revision": revision(settings),
        "settings": build_settings_catalog(settings, pending_settings(settings)),
        "backup": {"database_bytes": database_bytes, "database_limit_bytes": MAX_DATABASE_BYTES},
    }


def _draft(payload: dict):
    expected = payload.get("revision")
    if expected is not None and expected != revision(settings):
        raise HTTPException(status_code=409, detail="Settings changed in another session. Reload this page and review your changes again.")
    try:
        return validate_draft(settings, payload.get("settings"))
    except SettingsEditError as exc:
        raise HTTPException(status_code=exc.status, detail={"message": str(exc), "fields": exc.fields}) from exc


@app.post("/admin/settings/review")
async def review_local_settings(request: Request):
    payload = await _admin_json(request)
    async with controller.operation_lock:
        if controller.status()["restart_required"]:
            raise HTTPException(status_code=409, detail="Restart to apply the saved changes before editing again.")
        _, changes = _draft(payload)
        return {"changes": changes, "revision": revision(settings)}


async def _connection_check(candidate: Settings) -> dict:
    if not candidate.qbt_password or candidate.qbt_password.upper().startswith("REPLACE_"):
        return {"name": "qBittorrent connection", "ok": False, "message": "Set the qBittorrent password in Connection settings."}
    probe = QbtService()
    probe.settings = candidate
    try:
        await asyncio.to_thread(probe.test_connection)
        return {"name": "qBittorrent connection", "ok": True, "message": "Authentication and read-only API check succeeded. No torrents were changed."}
    except Exception:
        return {"name": "qBittorrent connection", "ok": False, "message": "Cannot authenticate or reach qBittorrent. Check its address, credentials, TLS and Docker network. No settings were saved."}


@app.post("/admin/test-connection")
async def test_qbt_connection(request: Request):
    payload = await _admin_json(request)
    async with controller.operation_lock:
        updates = payload.get("settings", {})
        if not isinstance(updates, dict) or set(updates) - {
            "qbt_host", "qbt_username", "qbt_password", "qbt_web_url",
            "qbt_request_timeout_seconds",
        }:
            raise HTTPException(status_code=422, detail="Only qBittorrent connection fields can be tested here.")
        candidate = _draft({**payload, "settings": updates})[0] if updates else settings
        return await _connection_check(candidate)


def _storage_checks() -> list[dict]:
    checks = []
    paths = [settings.data_dir, settings.event_dir, settings.local_staging_root,
             settings.nas_staging_root, settings.final_parent_prefix]
    if settings.final_parent_prefixes:
        paths.extend(path.strip() for path in settings.final_parent_prefixes.split(",") if path.strip())
    if settings.infected_action == "quarantine":
        paths.append(settings.quarantine_root)
    for name in dict.fromkeys(paths):
        try:
            accessible = Path(name).is_dir() and os.access(name, os.R_OK | os.W_OK | os.X_OK)
        except OSError:
            accessible = False
        checks.append({"name": f"Storage: {name}", "ok": accessible,
                       "message": "Directory is accessible. Also verify the intended host mount in Portainer." if accessible else "Directory is missing or not readable/writable/searchable. Check its host mount and UID/GID permissions."})
    return checks


async def _readiness_checks() -> list[dict]:
    checks = [await _connection_check(settings)]
    try:
        await asyncio.to_thread(service.scan_coordinator.scanner.require_healthy, force=True)
        checks.append({"name": "ClamD and definitions", "ok": True, "message": "Private scanner connection and definition freshness checks passed."})
    except Exception:
        checks.append({"name": "ClamD and definitions", "ok": False, "message": "Check sidecar health, definitions and the shared private socket in Portainer."})
    checks.extend(await asyncio.to_thread(_storage_checks))
    return checks


@app.post("/admin/checks")
async def check_setup():
    async with controller.operation_lock:
        return {"checks": await _readiness_checks()}


@app.post("/admin/pause")
async def pause_controller():
    async with controller.operation_lock:
        return controller.pause()


@app.post("/admin/resume")
async def resume_controller(request: Request):
    payload = await _admin_json(request)
    if payload.get("confirm_external_state") is not True:
        raise HTTPException(status_code=422, detail="Confirm mounts, qBittorrent state, and that the old controller is stopped")
    async with controller.operation_lock:
        try:
            controller.require_drained()
            checks = await _readiness_checks()
            if not all(check["ok"] for check in checks):
                raise HTTPException(status_code=409, detail={"message": "Resume checks failed; the controller remains paused.", "checks": checks})
            return controller.resume()
        except HTTPException:
            raise
        except Exception:
            logger.warning("Controller resume checks failed; controller remains paused")
            raise HTTPException(status_code=409, detail="Resume checks failed. Verify settings, ClamD health, qBittorrent access, and all content mounts; the controller remains paused.")


@app.post("/admin/settings")
async def save_local_settings(request: Request):
    payload = await _admin_json(request)
    async with controller.operation_lock:
        try:
            controller.require_drained()
            candidate, changes = _draft(payload)
            if not changes:
                raise HTTPException(status_code=422, detail="No settings changed. Blank secret replacement fields preserve the current value.")
            if any(change["advanced"] for change in changes) and payload.get("confirm_advanced") is not True:
                raise HTTPException(status_code=409, detail="Advanced scanner changes require explicit confirmation.")
            # Mark pending before the atomic replacement; a write error must not
            # allow the old process to resume with a different saved policy.
            write_private(Path(settings.data_dir) / "restart-required", b"settings changed\n")
            persist_settings(candidate)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except OSError:
            raise HTTPException(status_code=409, detail="Could not save settings. Check the local data directory permissions and free space; Intake remains paused. Restart and verify the saved values before resuming.")
    return {"restart_required": True, "message": "Settings saved locally. Restart the container to apply them."}


@app.post("/admin/deployment-notes")
async def save_deployment_notes(request: Request):
    payload = await _admin_json(request)
    notes = payload.get("notes")
    if not isinstance(notes, str):
        raise HTTPException(status_code=422, detail="Expected text notes")
    async with controller.operation_lock:
        write_private(Path(settings.data_dir) / "deployment-notes.txt", notes.encode())
    return {"saved": True}


@app.post("/admin/backup")
async def download_backup(request: Request):
    payload = await _admin_json(request)
    async with controller.operation_lock:
        try:
            controller.require_drained()
            temporary = tempfile.TemporaryDirectory(prefix=".backup-", dir=settings.data_dir)
            try:
                output = await _finish_thread(create_backup, settings, Path(temporary.name), str(payload.get("passphrase", "")))
            except BaseException:
                temporary.cleanup()
                raise
            return FileResponse(output, filename="torrent-intake.tibak", media_type="application/octet-stream", background=BackgroundTask(temporary.cleanup))
        except (OSError, ValueError, sqlite3.DatabaseError):
            raise HTTPException(status_code=409, detail="Backup failed. Wait for the controller to drain, apply pending settings first, use a 12+ character passphrase, and check local free space and SQLite configuration.")


@app.post("/admin/restore")
async def upload_backup(request: Request):
    if request.headers.get("X-TI-Confirm-Restore") != "replace-after-restart":
        raise HTTPException(status_code=422, detail="Explicit restore confirmation is required")
    async with controller.operation_lock:
        try:
            controller.require_drained()
            phrase = base64.b64decode(request.headers.get("X-TI-Backup-Passphrase", ""), validate=True).decode("utf-8")
            if len(phrase.encode()) > 1024:
                raise ValueError("Passphrase too long")
            with tempfile.TemporaryDirectory(prefix=".restore-upload-", dir=settings.data_dir) as temporary:
                directory = Path(temporary)
                upload = directory / "upload.tibak"
                size = 0
                with upload.open("xb") as handle:
                    os.chmod(upload, 0o600)
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > MAX_BACKUP_BYTES + 64:
                            raise HTTPException(status_code=413, detail="Backup exceeds the upload size limit")
                        handle.write(chunk)
                return await _finish_thread(stage_restore, upload, directory, settings, phrase)
        except (InvalidTag, ValueError, OSError, UnicodeError, zipfile.BadZipFile, sqlite3.DatabaseError):
            raise HTTPException(status_code=422, detail="Restore rejected. Check the passphrase, backup validity, local free space, and that the controller is drained. Existing data was not replaced.")


def _enrich_jobs(db: Session, jobs: list[Job]) -> list[Job]:
    # Restored connection details may be unreachable on the receiving host.
    # Keep the settings/restore UI available without waiting for those requests.
    if controller is None or not controller.paused:
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
    data_dir = Path(settings.data_dir)
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
            "settings_catalog": build_settings_catalog(settings, pending_settings(settings)),
            "max_custom_tags": MAX_CUSTOM_TAGS,
            "max_custom_tag_length": MAX_CUSTOM_TAG_LENGTH,
            "private_job_tag_prefix": PRIVATE_JOB_TAG_PREFIX,
        },
    )
