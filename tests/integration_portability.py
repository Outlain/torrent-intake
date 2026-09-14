"""Real HTTP/launcher round trip inside an isolated application test container."""
from __future__ import annotations

import base64
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


PASSPHRASE = "integration-only backup passphrase"


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
            raise AssertionError(process.stderr.read().decode())
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


def seed(directory, identifier):
    engine = create_engine(f"sqlite:///{directory / 'torrent_intake.db'}")
    with Session(engine) as db:
        db.add(Job(id=identifier, magnet_uri="magnet:?private=integration-passkey", final_parent="/downloads/Movies",
                   staging_preference="nas", staging_root_initial="/downloads/torrent-intake/staging",
                   managed_tag="torrent_intake", unique_tag=f"ti_job_{identifier}"))
        db.commit()
        db.add(ScanRun(job_id=identifier, root_path="/downloads/torrent-intake/staging"))
        db.add(ScanFile(job_id=identifier, relative_path="movie.mkv", size_bytes=123, mtime_ns=456, status="clean"))
        db.commit()
    engine.dispose()


def ids(directory):
    connection = sqlite3.connect(directory / "torrent_intake.db")
    try:
        return [row[0] for row in connection.execute("SELECT id FROM jobs")]
    finally:
        connection.close()


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
            assert request(18000, "/qbt/categories")[0] == 503
            assert request(18000, "/admin/settings", token=token, payload={"settings": {"infected_action": "delete"}})[0] == 409
            assert request(18000, "/admin/settings", token=token, payload={"settings": {"app_name": "unused"}})[0] == 409
            assert request(18000, "/admin/settings", token=token, payload={"settings": {"qbt_host": "http://other"}})[0] == 409
            assert request(18000, "/admin/settings", token=token, payload={"settings": {"per_job_scan_workers": 100}})[0] == 422
            assert request(18000, "/admin/settings", token=token, payload={"settings": {
                "ui_title": "Portable title", "qbt_password": "portable-secret", "polling_interval_seconds": 60,
            }})[0] == 200
            assert request(18000, "/admin/backup", token=token, payload={"passphrase": PASSPHRASE})[0] == 409
            stop(first)
            processes.remove(first)
            first = launch(source, 18000)  # Explicit old environment values were saved.
            processes.append(first)
            config = json.loads((source / "settings.json").read_text())["settings"]
            assert config["qbt_host"] == "http://gluetun:8080"
            assert config["qbt_password"] == "portable-secret"
            seed(source, "original")
            assert request(18000, "/admin/deployment-notes", token=token, payload={"notes": "host mounts and compose settings"})[0] == 200
            status, backup = request(18000, "/admin/backup", token=token, payload={"passphrase": PASSPHRASE})
            assert status == 200, backup
            assert b"portable-secret" not in backup and b"integration-passkey" not in backup
            # A second process cannot run against the same local data directory.
            duplicate = subprocess.run([sys.executable, "-m", "app.launcher", "true"], env={**os.environ, "TI_DATA_DIR": str(source)}, capture_output=True, timeout=5)
            assert duplicate.returncode != 0
            assert b"already using" in duplicate.stderr
            stop(first)
            processes.remove(first)

            second = launch(destination, 18001, TI_UI_TITLE="Receiving environment title")
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
            assert status == 200, body
            assert ids(destination) == ["previous"], "must not replace a live database"
            stop(second)
            processes.remove(second)
            second = launch(destination, 18001, TI_UI_TITLE="Receiving environment title")
            processes.append(second)
            assert ids(destination) == ["original"]
            assert request(18001, "/ui")[0] == 200  # No blocking qB lookup while paused.
            state = json.loads(request(18001, "/controller/status")[1])
            assert state["paused"] and state["drained"] and not state["restore_pending"]
            values = json.loads((destination / "settings.json").read_text())["settings"]
            assert values["ui_title"] == "Receiving environment title"
            assert values["qbt_password"] == "portable-secret"
            assert values["database_url"] == f"sqlite:///{destination / 'torrent_intake.db'}"
            assert (destination / "admin-token").read_text().strip() == new_token
            assert len(list(destination.glob("before-restore-*"))) == 1
            assert request(18001, "/admin/resume", token=new_token, payload={"confirm_external_state": True})[0] == 409
            assert json.loads(request(18001, "/controller/status")[1])["paused"]
            print("PASS real HTTP encrypted backup/restore, offline replacement, environment precedence, private token, controller lock, and paused recovery")
        finally:
            for process in processes:
                stop(process)


if __name__ == "__main__":
    main()
