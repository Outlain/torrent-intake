"""One bounded, restart-safe optional hook after verified clean promotion.

Only the promotion transaction creates pending work. A committed running claim
is deliberately never retried automatically: an interrupted external program
may already have made changes which this application cannot roll back.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import os
from pathlib import Path
import signal
import stat
import sys

from sqlalchemy import or_, select, update

from .config import get_settings
from .copy_action import validate_destination as validate_copy_destination
from .db import SessionLocal
from .event_writer import emit_event
from .models import Job, ScanFile
from .paths import canonical_final_parent
from .qbt import QbtService
from .storage import StorageUnavailable, require_final_storage
from .torrent_guard import TorrentSafetyGuard

logger = logging.getLogger(__name__)
HOOK_ROOT = Path("/hooks")
OUTPUT_LIMIT_BYTES = 64 * 1024
RECHECK_SECONDS = 15
PROCESS_TERMINATION_SECONDS = 2


class HookNotReady(RuntimeError):
    """No subprocess has started; a safe read-only check may be tried later."""


@dataclass(frozen=True)
class HookClaim:
    job_id: str
    script: str | None
    source: str
    torrent_hash: str
    torrent_name: str
    attempt: int
    kind: str = "script"
    destination: str | None = None

    @property
    def argv(self) -> list[str]:
        if self.kind == "copy":
            if not self.destination:
                raise RuntimeError("Built-in copy claim has no destination")
            command = [sys.executable, "-m", "app.copy_action", "--destination", self.destination]
        elif self.kind == "script" and self.script:
            command = [self.script]
        else:
            raise RuntimeError("Unknown post-promotion action or missing script")
        return command + [
            "--source", self.source,
            "--torrent-hash", self.torrent_hash,
            "--torrent-name=" + self.torrent_name,
            "--job-id", self.job_id,
        ]


def queue_promotion_hook(job: Job, settings) -> bool:
    """Called only inside the successful promotion transaction; never commits."""
    if job.hook_status is not None:
        return False
    if job.state != "done" or not job.promoted_at or not job.scan_completed_at:
        return False
    copy_enabled = getattr(settings, "post_promotion_copy_enabled", False)
    # Preserve a previous action kind and copy target across explicit retries.
    # A new default applies only to newly promoted jobs, never existing work.
    kind = job.hook_kind or ("script" if job.hook_script else None) or ("copy" if copy_enabled else "script")
    if kind == "copy":
        destination = job.hook_destination or getattr(settings, "post_promotion_copy_destination", None)
        if not copy_enabled or not destination:
            return False
        job.hook_destination = destination
        job.hook_script = None
    elif kind == "script":
        if not getattr(settings, "post_promotion_enabled", False) or not settings.post_promotion_script:
            return False
        job.hook_script = settings.post_promotion_script
        job.hook_destination = None
    else:
        return False
    job.hook_status = "pending"
    job.hook_kind = kind
    job.hook_due_at = datetime.utcnow() + timedelta(seconds=settings.post_promotion_delay_seconds)
    job.hook_error = None
    job.hook_output = None
    job.hook_exit_code = None
    job.hook_started_at = None
    job.hook_finished_at = None
    job.hook_attempts = job.hook_attempts or 0
    return True


def _no_symlinks(path: Path) -> os.stat_result:
    if not path.is_absolute() or ".." in path.parts:
        raise RuntimeError(f"hook path must be absolute and traversal-free: {path}")
    current = Path(path.anchor)
    info = current.lstat()
    for part in path.parts[1:]:
        current /= part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError(f"hook path contains a symbolic link: {current}")
    return info


def _filesystem_manifest(source: Path, stop_event: asyncio.Event | None = None) -> dict[str, int]:
    info = _no_symlinks(source)
    if stat.S_ISREG(info.st_mode):
        if not os.access(source, os.R_OK):
            raise HookNotReady(f"promoted content is not readable: {source}")
        return {".": info.st_size}
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"promoted content is not a regular file or directory: {source}")
    manifest: dict[str, int] = {}

    def enumeration_failed(error: OSError) -> None:
        raise HookNotReady(f"promoted content cannot be enumerated: {error}") from error

    for directory, directories, files in os.walk(source, followlinks=False, onerror=enumeration_failed):
        if stop_event is not None and stop_event.is_set():
            raise HookNotReady("Controller stopped during hook preflight; hook remains queued")
        current = Path(directory)
        if not os.access(current, os.R_OK | os.X_OK):
            raise HookNotReady(f"promoted content directory is not accessible: {current}")
        for name in directories:
            candidate = current / name
            if not stat.S_ISDIR(candidate.lstat().st_mode):
                raise RuntimeError(f"promoted content contains a symbolic link or special directory: {candidate}")
        for name in files:
            if stop_event is not None and stop_event.is_set():
                raise HookNotReady("Controller stopped during hook preflight; hook remains queued")
            candidate = current / name
            info = candidate.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeError(f"promoted content contains a symbolic link or special file: {candidate}")
            if not os.access(candidate, os.R_OK):
                raise HookNotReady(f"promoted content is not readable: {candidate}")
            manifest[candidate.relative_to(source).as_posix()] = info.st_size
    return manifest


class PostPromotionRunner:
    def __init__(self, *, settings=None, qbt=None, session_factory=None) -> None:
        self.settings = settings if settings is not None else get_settings()
        self.qbt = qbt if qbt is not None else QbtService()
        self.session_factory = session_factory if session_factory is not None else SessionLocal
        self.guard = TorrentSafetyGuard()

    def recover_interrupted(self) -> int:
        with self.session_factory() as db:
            result = db.execute(
                update(Job).where(Job.hook_status == "running").values(
                    hook_status="interrupted",
                    hook_finished_at=datetime.utcnow(),
                    hook_error="Controller restarted during a hook attempt; external effects are unknown. Review before an explicit retry.",
                )
            )
            db.commit()
            return result.rowcount

    def _validate(self, db, job: Job, stop_event: asyncio.Event | None = None) -> tuple[str | None, str]:
        if job.state != "done" or not job.is_terminal or not job.scan_completed_at or not job.promoted_at:
            raise RuntimeError("hook requires a successfully scanned and promoted done job")
        if not job.qbt_hash or not job.content_path:
            raise RuntimeError("promoted job has no verified torrent hash or content path")
        kind = job.hook_kind or "script"  # Existing queued script rows predate action kinds.
        script = None
        if kind == "copy":
            if not job.hook_destination:
                raise RuntimeError("queued built-in copy has no saved destination")
            destination = Path(job.hook_destination)
            try:
                validate_copy_destination(destination)
            except OSError as exc:
                raise HookNotReady(f"copy destination/mount is unavailable: {exc}") from exc
            source = Path(job.content_path)
            if source.is_relative_to(destination) or destination.is_relative_to(source):
                raise RuntimeError("Copy source and destination must not be identical or nested")
        elif kind == "script":
            if not job.hook_script or job.hook_script != self.settings.post_promotion_script:
                raise RuntimeError("queued hook does not match the current deployment script; review and explicitly retry")
            script = Path(job.hook_script)
            if not script.is_absolute() or not script.is_relative_to(HOOK_ROOT) or script == HOOK_ROOT:
                raise RuntimeError("hook script must be an absolute executable path inside /hooks")
            try:
                info = _no_symlinks(script)
            except OSError as exc:
                raise RuntimeError(f"configured hook script is unavailable: {exc}") from exc
            if not stat.S_ISREG(info.st_mode) or not os.access(script, os.X_OK):
                raise RuntimeError("configured hook script must be an executable regular file")
        else:
            raise RuntimeError("Unknown post-promotion action kind")

        expected_parent = Path(canonical_final_parent(job.final_parent, self.settings))
        source = Path(job.content_path)
        if not source.is_absolute() or source == expected_parent or not source.is_relative_to(expected_parent):
            raise RuntimeError("promoted content is not strictly inside its allowed final destination")
        try:
            require_final_storage(self.settings, str(expected_parent))
            _no_symlinks(expected_parent)
            actual = _filesystem_manifest(source, stop_event)
        except (OSError, StorageUnavailable) as exc:
            raise HookNotReady(f"promoted content/mount is unavailable: {exc}") from exc
        scanned = list(db.scalars(select(ScanFile).where(ScanFile.job_id == job.id)))
        if not scanned or any(item.status != "clean" for item in scanned):
            raise RuntimeError("promoted content has no complete clean scan manifest")
        expected = {item.relative_path: item.size_bytes for item in scanned}
        if actual != expected:
            raise RuntimeError("promoted content no longer matches the clean scan manifest (file paths or sizes changed)")

        # Do the live qB check last, after any potentially large tree walk, so a
        # moving/incomplete/reassigned torrent can never reach the executor.
        try:
            torrent = self.qbt.get_torrent(job.qbt_hash)
        except Exception as exc:
            raise HookNotReady(f"qBittorrent verification is temporarily unavailable: {exc}") from exc
        if torrent is None:
            raise HookNotReady("qBittorrent torrent is temporarily unavailable")
        self.guard.validate_common(db, job, torrent, require_paused=False, require_complete=False)
        qbt_state = str(getattr(torrent, "state", "") or "").lower()
        if not self.guard.is_complete(torrent) or qbt_state.startswith("checking"):
            raise HookNotReady("torrent is incomplete, moving, checking, or unavailable; hook remains queued")
        if str(getattr(torrent, "content_path", "") or "") != str(source):
            raise RuntimeError("qBittorrent content path changed after promotion")
        verified = self.guard.validate_destination(db, job, torrent, require_paused=False)
        if verified != source:
            raise RuntimeError("promoted content path is not canonical")
        return str(script) if script is not None else None, str(verified)

    def claim_next(self, stop_event: asyncio.Event | None = None) -> HookClaim | None:
        copy_enabled = getattr(self.settings, "post_promotion_copy_enabled", False)
        if not (self.settings.post_promotion_enabled or copy_enabled) or (stop_event is not None and stop_event.is_set()):
            return None
        allowed_kind = Job.hook_kind == "copy" if copy_enabled else or_(Job.hook_kind == "script", Job.hook_kind.is_(None))
        with self.session_factory() as db:
            jobs = list(db.scalars(
                select(Job).where(Job.hook_status == "pending", Job.hook_due_at <= datetime.utcnow(), allowed_kind)
                .order_by(Job.hook_due_at, Job.id).limit(20)
            ))
            for job in jobs:
                if stop_event is not None and stop_event.is_set():
                    return None
                try:
                    script, source = self._validate(db, job, stop_event)
                except HookNotReady as exc:
                    db.execute(update(Job).where(Job.id == job.id, Job.hook_status == "pending").values(
                        hook_error=str(exc)[:2000],
                        hook_due_at=datetime.utcnow() + timedelta(seconds=RECHECK_SECONDS),
                    ))
                    db.commit()
                    continue
                except Exception as exc:
                    db.execute(update(Job).where(Job.id == job.id, Job.hook_status == "pending").values(
                        hook_status="failed", hook_error=str(exc)[:2000], hook_finished_at=datetime.utcnow(),
                    ))
                    db.commit()
                    logger.warning("Post-promotion hook preflight failed for job %s: %s", job.id, exc)
                    continue
                if stop_event is not None and stop_event.is_set():
                    return None
                attempt = (job.hook_attempts or 0) + 1
                claim = HookClaim(job.id, script, source, job.qbt_hash, job.torrent_name or "", attempt,
                                  job.hook_kind or "script", job.hook_destination)
                result = db.execute(update(Job).where(Job.id == job.id, Job.hook_status == "pending").values(
                    hook_status="running", hook_started_at=datetime.utcnow(), hook_finished_at=None,
                    hook_attempts=attempt, hook_error=None, hook_output=None, hook_exit_code=None,
                ))
                db.commit()  # Durable claim always precedes the external side effect.
                if result.rowcount == 1:
                    return claim
        return None

    def _finish(self, claim: HookClaim, status: str, error: str | None, output: str, exit_code: int | None) -> None:
        with self.session_factory() as db:
            result = db.execute(update(Job).where(
                Job.id == claim.job_id, Job.hook_status == "running", Job.hook_attempts == claim.attempt,
            ).values(
                hook_status=status, hook_finished_at=datetime.utcnow(), hook_error=error,
                hook_output=output, hook_exit_code=exit_code,
            ))
            db.commit()
        if result.rowcount:
            try:
                emit_event(
                    f"post_promotion_hook_{status}", "info" if status == "succeeded" else "warning",
                    f"Post-promotion hook {status}",
                    job_id=claim.job_id, torrent_hash=claim.torrent_hash,
                    hook_attempt=claim.attempt, hook_exit_code=exit_code, error=error,
                    hook_kind=claim.kind, copy_destination=claim.destination,
                )
            except Exception:
                logger.exception("Could not emit hook outcome event for job %s", claim.job_id)

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> bool:
        # A separate session gives the script and its ordinary descendants one
        # process group. Always kill remaining descendants, even if the leader
        # already exited or ignored SIGTERM.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(asyncio.shield(process.wait()), PROCESS_TERMINATION_SECONDS)
        except asyncio.TimeoutError:
            pass
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(asyncio.shield(process.wait()), PROCESS_TERMINATION_SECONDS)
        except asyncio.TimeoutError:
            # A daemonized child can leave the process group but retain stdout.
            # asyncio can wait for pipe EOF even after the leader has exited.
            # Process has no public pipe-close API; closing its transport is
            # necessary to keep controller pause/shutdown bounded in that case.
            process._transport.close()
            return True
        return False

    async def run_claim(self, claim: HookClaim, stop_event: asyncio.Event) -> None:
        process = None
        tasks: list[asyncio.Task] = []
        captured = bytearray()
        truncated = False
        incomplete_output = False
        status, error, exit_code = "failed", None, None

        async def capture_output() -> None:
            nonlocal truncated
            while chunk := await process.stdout.read(8192):
                remaining = OUTPUT_LIMIT_BYTES - len(captured)
                captured.extend(chunk[:remaining])
                truncated |= len(chunk) > remaining

        try:
            if stop_event.is_set():
                status, error = "interrupted", "Controller stopped before the claimed hook could start; explicit retry required."
                return
            process = await asyncio.create_subprocess_exec(
                *claim.argv, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                cwd=str(Path(__file__).resolve().parent.parent) if claim.kind == "copy" else "/tmp",
                env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "TMPDIR": "/tmp"},
                start_new_session=True,
            )
            reader = asyncio.create_task(capture_output())
            waiter = asyncio.create_task(process.wait())
            stopped = asyncio.create_task(stop_event.wait())
            tasks = [reader, waiter, stopped]
            done, _ = await asyncio.wait(
                [waiter, stopped], timeout=self.settings.post_promotion_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stopped in done:
                status, error = "interrupted", "Controller stopped during hook execution; review external effects before an explicit retry."
            elif waiter not in done:
                status, error = "failed", f"Hook timed out after {self.settings.post_promotion_timeout_seconds} seconds; review external effects before retrying."
            else:
                exit_code = waiter.result()
                status = "succeeded" if exit_code == 0 else "failed"
                error = None if exit_code == 0 else f"Hook exited with status {exit_code}."
        except asyncio.CancelledError:
            status, error = "interrupted", "Hook runner was cancelled; review external effects before an explicit retry."
            raise
        except Exception as exc:
            status, error = "failed", f"Hook execution failed: {exc}"[:2000]
        finally:
            if process is not None:
                incomplete_output = await self._terminate(process)
                exit_code = process.returncode
                if tasks:
                    try:
                        await asyncio.wait_for(asyncio.shield(tasks[0]), PROCESS_TERMINATION_SECONDS)
                    except asyncio.TimeoutError:
                        incomplete_output = True
                        tasks[0].cancel()
                        process._transport.close()
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            output = captured.decode("utf-8", errors="replace")
            suffix = "\n[output truncated]\n" if truncated else ""
            if incomplete_output:
                suffix += "\n[output incomplete: a background process kept the pipe open]\n"
                if status == "succeeded":
                    status, error = "failed", "Hook exited but a background process kept its output pipe open; review external effects before retrying."
            if suffix:
                output = output[:OUTPUT_LIMIT_BYTES - len(suffix)] + suffix
            await asyncio.to_thread(self._finish, claim, status, error, output, exit_code)

    async def run(self, stop_event: asyncio.Event) -> None:
        try:
            recovered = await asyncio.to_thread(self.recover_interrupted)
            if recovered:
                logger.warning("Marked %s interrupted post-promotion hook attempts; explicit retry required", recovered)
        except Exception:
            # Do not start external actions without successful recovery.
            logger.exception("Post-promotion hook recovery failed; hook runner disabled until controller restart")
            return
        while not stop_event.is_set():
            try:
                claim = await asyncio.to_thread(self.claim_next, stop_event)
                if claim is not None:
                    await self.run_claim(claim, stop_event)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Post-promotion hook cycle failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
