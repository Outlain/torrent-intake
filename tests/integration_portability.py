"""Real HTTP/launcher round trip inside an isolated application test container."""
from __future__ import annotations

import base64
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Job, ScanRun, ScanFile
from test_torrent_files import torrent


PASSPHRASE = "integration-only backup passphrase"
QBT_PASSWORD = "portable-secret"
PRIVATE_PASSKEY = "integration-passkey"
NAS_LOCATIONS = [
    {"id": "primary", "label": "Portable main NAS", "path": "/downloads/torrent-intake/staging",
     "mount_marker": "/downloads/.intake-mounted"},
    {"id": "archive", "label": "Portable archive NAS", "path": "/nas-archive/intake",
     "mount_marker": "/nas-archive/.intake-mounted"},
]
HOOK_SCRIPT = "/hooks/portable-post-promotion.sh"
COPY_DESTINATION = "/copy-target/portable-copies"
PINNED_COPY_DESTINATION = "/copy-target/previous-copies"
JOB_SNAPSHOT_FIELDS = (
    "id", "state", "is_terminal", "staging_preference", "staging_actual",
    "staging_root_initial", "staging_root_actual", "nas_staging_id", "nas_staging_label",
    "nas_staging_path", "nas_mount_marker", "scan_completed_at", "promoted_at",
    "hook_status", "hook_due_at", "hook_started_at", "hook_finished_at", "hook_error",
    "hook_output", "hook_script", "hook_attempts", "hook_exit_code", "hook_kind", "hook_destination",
)


def request(port, path, *, token=None, payload=None, raw=None, headers=None):
    fields = dict(headers or {})
    if token:
        fields["X-TI-Admin-Token"] = token
    if payload is not None:
        fields["Content-Type"] = "application/json"
        raw = json.dumps(payload).encode()
    call = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=raw, headers=fields)
    try:
        with urllib.request.urlopen(call, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def launch(directory, port, **overrides):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("TI_")}
    environment.update(TI_DATA_DIR=str(directory), **overrides)
    process = subprocess.Popen(
        [sys.executable, "-m", "app.launcher", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            diagnostic = process.stderr.read().decode()
            for secret in (PASSPHRASE, QBT_PASSWORD, PRIVATE_PASSKEY):
                diagnostic = diagnostic.replace(secret, "[redacted]")
            raise AssertionError(diagnostic)
        try:
            if request(port, "/controller/status")[0] == 200:
                return process
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.1)
    stop(process)
    raise AssertionError("test server did not start")


def stop(process):
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise AssertionError("test server failed to stop cooperatively")
    finally:
        process.stderr.close()


def seed(directory, identifier, *, local_fallback=False, hook_kind="script"):
    location = NAS_LOCATIONS[0 if local_fallback else 1]
    staging_root = "/staging/local" if local_fallback else location["path"]
    completed_at = datetime(2026, 1, 2, 3, 4, 5)
    engine = create_engine(f"sqlite:///{directory / 'torrent_intake.db'}")
    with Session(engine) as db:
        job = Job(id=identifier, magnet_uri=f"magnet:?private={PRIVATE_PASSKEY}", final_parent="/downloads/Movies",
                  torrent_file_name="example.torrent", torrent_file_data=torrent(),
                  staging_preference="local" if local_fallback else "nas",
                  staging_actual="local" if local_fallback else "nas",
                  staging_root_initial=staging_root, staging_root_actual=staging_root,
                  nas_staging_id=location["id"], nas_staging_label=location["label"],
                  nas_staging_path=location["path"], nas_mount_marker=location["mount_marker"],
                  state="waiting_for_local_space" if local_fallback else "done", is_terminal=not local_fallback,
                  managed_tag="torrent_intake", unique_tag=f"ti_job_{identifier}")
        if not local_fallback:
            job.scan_completed_at = job.promoted_at = completed_at
            job.hook_status = "failed"
            job.hook_due_at = job.hook_started_at = job.hook_finished_at = completed_at
            job.hook_kind = hook_kind
            job.hook_error = f"Portable test {hook_kind} failed after promotion"
            job.hook_output = f"Portable test {hook_kind} output"
            job.hook_script = HOOK_SCRIPT if hook_kind == "script" else None
            job.hook_destination = PINNED_COPY_DESTINATION if hook_kind == "copy" else None
            job.hook_attempts = 2
            job.hook_exit_code = 7
        db.add(job)
        db.commit()
        db.add(ScanRun(job_id=identifier, root_path=staging_root))
        db.add(ScanFile(job_id=identifier, relative_path="movie.mkv", size_bytes=123, mtime_ns=456, status="clean"))
        db.commit()
    engine.dispose()


def ids(directory):
    connection = sqlite3.connect(directory / "torrent_intake.db")
    try:
        return [row[0] for row in connection.execute("SELECT id FROM jobs ORDER BY id")]
    finally:
        connection.close()


def job_snapshots(directory):
    """Compare portable state without reading credentials or tracker metadata."""
    with sqlite3.connect(directory / "torrent_intake.db") as connection:
        connection.row_factory = sqlite3.Row
        return {row["id"]: dict(row) for row in connection.execute(
            f"SELECT {','.join(JOB_SNAPSHOT_FIELDS)} FROM jobs ORDER BY id"
        )}


def main():
    assert os.getuid() != 0, "run this integration test as the image's non-root user"
    processes = []
    with tempfile.TemporaryDirectory(prefix="ti-portability-") as temporary:
        root = Path(temporary)
        source, destination = root / "source", root / "destination"
        try:
            first = launch(source, 18000, TI_QBT_HOST="http://gluetun:8080")
            processes.append(first)
            token = (source / "admin-token").read_text().strip()
            assert json.loads(request(18000, "/controller/status")[1])["drained"]
            assert request(18000, "/ui")[0] == 200
            assert request(18000, "/admin/status")[0] == 403
            backup_status = json.loads(request(18000, "/admin/status", token=token)[1])["backup"]
            assert backup_status["database_bytes"] > 0
            assert backup_status["database_limit_bytes"] == 512 * 1024 * 1024
            assert request(18000, "/jobs", payload={})[0] == 503
            assert request(18000, "/jobs/torrent", raw=b"invalid", headers={"Content-Type": "application/octet-stream"})[0] == 503
            assert request(18000, "/qbt/categories")[0] == 503
            assert request(18000, "/admin/settings", token=token, payload={"settings": {"infected_action": "delete"}})[0] == 409
            assert request(18000, "/admin/settings", token=token, payload={"settings": {"app_name": "unused"}})[0] == 409
            assert request(18000, "/admin/settings", token=token, payload={"settings": {"qbt_host": "http://other"}})[0] == 409
            assert request(18000, "/admin/settings", token=token, payload={"settings": {"per_job_scan_workers": 100}})[0] == 422
            assert request(18000, "/admin/settings", token=token, payload={"confirm_advanced": True, "settings": {
                "ui_title": "Portable title", "qbt_password": QBT_PASSWORD, "polling_interval_seconds": 60,
                "nas_staging_locations": NAS_LOCATIONS, "default_nas_staging_id": "archive",
                "post_promotion_copy_enabled": True, "post_promotion_copy_destination": COPY_DESTINATION,
                "post_promotion_delay_seconds": 17,
                "post_promotion_timeout_seconds": 900,
            }})[0] == 200
            assert request(18000, "/admin/backup", token=token, payload={"passphrase": PASSPHRASE})[0] == 409
            stop(first)
            processes.remove(first)
            first = launch(source, 18000)  # Explicit old environment values were saved.
            processes.append(first)
            config = json.loads((source / "settings.json").read_text())["settings"]
            assert config["qbt_host"] == "http://gluetun:8080"
            assert config["qbt_password"] == QBT_PASSWORD
            assert config["nas_staging_locations"] == NAS_LOCATIONS
            assert config["default_nas_staging_id"] == "archive"
            assert config["post_promotion_copy_enabled"] and config["post_promotion_copy_destination"] == COPY_DESTINATION
            assert not config["post_promotion_enabled"] and config["post_promotion_script"] is None
            seed(source, "original")
            seed(source, "copied-original", hook_kind="copy")
            seed(source, "local-fallback", local_fallback=True)
            expected_job_snapshots = job_snapshots(source)
            assert request(18000, "/admin/deployment-notes", token=token, payload={"notes": "host mounts and compose settings"})[0] == 200
            status, backup = request(18000, "/admin/backup", token=token, payload={"passphrase": PASSPHRASE})
            assert status == 200, f"backup request failed ({status})"
            assert QBT_PASSWORD.encode() not in backup and PRIVATE_PASSKEY.encode() not in backup
            # A second process cannot run against the same local data directory.
            duplicate = subprocess.run([sys.executable, "-m", "app.launcher", "true"], env={**os.environ, "TI_DATA_DIR": str(source)}, capture_output=True, timeout=5)
            assert duplicate.returncode != 0
            assert b"already using" in duplicate.stderr
            stop(first)
            processes.remove(first)

            second = launch(destination, 18001, TI_UI_TITLE="Receiving environment title", TI_DEFAULT_NAS_STAGING_ID="primary")
            processes.append(second)
            new_token = (destination / "admin-token").read_text().strip()
            assert new_token != token
            seed(destination, "previous")
            headers = {"Content-Type": "application/octet-stream", "X-TI-Confirm-Restore": "replace-after-restart",
                       "X-TI-Backup-Passphrase": base64.b64encode(b"incorrect test passphrase").decode()}
            assert request(18001, "/admin/restore", token=new_token, raw=backup, headers=headers)[0] == 422
            assert ids(destination) == ["previous"]
            headers["X-TI-Backup-Passphrase"] = base64.b64encode(PASSPHRASE.encode()).decode()
            status, body = request(18001, "/admin/restore", token=new_token, raw=backup, headers=headers)
            assert status == 200, f"restore request failed ({status})"
            assert ids(destination) == ["previous"], "must not replace a live database"
            stop(second)
            processes.remove(second)
            second = launch(destination, 18001, TI_UI_TITLE="Receiving environment title", TI_DEFAULT_NAS_STAGING_ID="primary")
            processes.append(second)
            assert ids(destination) == ["copied-original", "local-fallback", "original"]
            assert job_snapshots(destination) == expected_job_snapshots, "pinned NAS, copy destination and action state must survive restore unchanged"
            status, body = request(18001, "/jobs")
            assert status == 200
            restored_jobs = {job["id"]: job for job in json.loads(body)}
            for identifier, expected in expected_job_snapshots.items():
                for name in ("staging_actual", "nas_staging_id", "nas_staging_label", "nas_staging_path",
                             "hook_status", "hook_error", "hook_output", "hook_exit_code", "hook_kind", "hook_destination"):
                    assert restored_jobs[identifier][name] == expected[name], f"restored job API changed {name}"
            with sqlite3.connect(destination / "torrent_intake.db") as db:
                assert db.execute("SELECT torrent_file_name,torrent_file_data FROM jobs WHERE id='original'").fetchone() == ("example.torrent", torrent())
            assert request(18001, "/ui")[0] == 200  # No blocking qB lookup while paused.
            state = json.loads(request(18001, "/controller/status")[1])
            assert state["paused"] and state["drained"] and not state["restore_pending"]
            values = json.loads((destination / "settings.json").read_text())["settings"]
            assert values["ui_title"] == "Receiving environment title"
            assert values["qbt_password"] == QBT_PASSWORD
            assert values["nas_staging_locations"] == NAS_LOCATIONS
            assert values["default_nas_staging_id"] == "primary", "receiving environment wins without retargeting existing jobs"
            assert values["post_promotion_copy_enabled"] and values["post_promotion_copy_destination"] == COPY_DESTINATION
            assert not values["post_promotion_enabled"] and values["post_promotion_script"] is None
            assert restored_jobs["copied-original"]["hook_destination"] == PINNED_COPY_DESTINATION
            assert restored_jobs["copied-original"]["hook_destination"] != values["post_promotion_copy_destination"], "changing the default must not retarget a saved copy"
            assert values["post_promotion_delay_seconds"] == 17
            assert values["post_promotion_timeout_seconds"] == 900
            assert not Path(HOOK_SCRIPT).exists(), "script executables must not be included in portable backups"
            assert values["database_url"] == f"sqlite:///{destination / 'torrent_intake.db'}"
            assert (destination / "admin-token").read_text().strip() == new_token
            assert len(list(destination.glob("before-restore-*"))) == 1
            assert request(18001, "/admin/resume", token=new_token, payload={"confirm_external_state": True})[0] == 409
            assert json.loads(request(18001, "/controller/status")[1])["paused"]
            print("PASS real HTTP encrypted backup/restore, UI-saved built-in copy without script configuration, pinned copy destination and script history, named NAS/defaults/markers, pinned local/NAS jobs, offline replacement, environment precedence, private token, controller lock, and paused recovery")
        finally:
            for process in processes:
                stop(process)


if __name__ == "__main__":
    main()
