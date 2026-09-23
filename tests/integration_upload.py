"""Multipart Intake -> real qBittorrent; only inside a --network none test container."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

import qbittorrentapi
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
import uvicorn

from app.config import Settings
from app.db import Base, get_db
from app.metainfo import parse_torrent
from app.models import Job
from app.qbt import QbtService
from test_torrent_files import multipart, torrent, v1_info, v2_info


def main():
    assert os.getuid() != 0
    with tempfile.TemporaryDirectory(prefix="ti-upload-integration-") as temporary:
        root = Path(temporary)
        os.environ["TI_DATA_DIR"] = str(root / "app-data")
        # Tests are offline. Disabling loopback authentication is confined to
        # this disposable profile and must never be used in a deployment.
        config = root / "qBittorrent" / "config"
        config.mkdir(parents=True)
        (config / "qBittorrent.conf").write_text(
            "[LegalNotice]\nAccepted=true\n[Preferences]\nWebUI\\LocalHostAuth=false\nWebUI\\Address=127.0.0.1\n"
            "WebUI\\HostHeaderValidation=false\nConnection\\UPnP=false\n")
        process = subprocess.Popen(["qbittorrent-nox", f"--profile={root}", "--webui-port=18082"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        server, thread = None, None
        engine = create_engine(f"sqlite:///{root / 'jobs.db'}")
        try:
            client = qbittorrentapi.Client(host="http://127.0.0.1:18082", REQUESTS_ARGS={"timeout": 5})
            for _ in range(100):
                try:
                    version = client.app_version()
                    break
                except qbittorrentapi.APIError:
                    assert process.poll() is None, process.stdout.read().decode()
                    time.sleep(0.1)
            else:
                raise AssertionError("qBittorrent did not start")

            import app.main as application
            Base.metadata.create_all(engine)
            application.service.settings = Settings(_env_file=None, local_staging_root=str(root / "staging"))
            application.service.qbt = QbtService.__new__(QbtService)
            application.service.qbt._with_client = lambda operation: operation(client)
            # This test checks the submission transport and durability. Staging
            # scheduling/scanning are tested independently; no worker is started.
            application.service._evaluate_staging_now = lambda db, job: None

            def database():
                with Session(engine) as db:
                    yield db

            application.app.dependency_overrides[get_db] = database
            server = uvicorn.Server(uvicorn.Config(application.app, host="127.0.0.1", port=18083,
                                                    lifespan="off", log_level="warning"))
            thread = threading.Thread(target=server.run)
            thread.start()
            for _ in range(100):
                if server.started:
                    break
                time.sleep(0.1)
            assert server.started

            def send(path, data, content_type):
                request = urllib.request.Request("http://127.0.0.1:18083" + path, data=data,
                                                 headers={"Content-Type": content_type})
                try:
                    with urllib.request.urlopen(request, timeout=30) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as exc:
                    return exc.code, json.load(exc)

            for number, info in enumerate((v1_info(), v2_info(), {**v1_info(), **v2_info()})):
                info[b"name"] = f"example-{number}.txt".encode()
                if b"file tree" in info:
                    info[b"file tree"] = {info[b"name"]: info[b"file tree"][b"example.txt"]}
                data = torrent(info)
                settings = {"final_parent": "/downloads/Shows", "final_category": "Shows",
                            "staging_preference": "local", "custom_tags": ["Review"]}
                body = multipart([("settings", None, json.dumps(settings).encode()), ("file", "example.torrent", data)])
                status, result = send("/jobs/torrent", body, "multipart/form-data; boundary=test-boundary")
                assert status == 200, (number, result)
                identifier = result["id"]
                assert "torrent_file_data" not in result
                with Session(engine) as db:
                    job = db.get(Job, identifier)
                    assert job.torrent_file_data == data
                    # qB accepts uploads asynchronously. With the worker disabled
                    # above, exercise its existing hash-resolution retry here.
                    deadline = time.monotonic() + 10
                    while not job.qbt_hash and job.state == "waiting_for_qbt_hash" and time.monotonic() < deadline:
                        time.sleep(0.1)
                        application.service._resolve_hash_for_job(db, job)
                    assert job.qbt_hash, (number, "hash resolution failed", job.state, job.last_error)
                    matches = client.torrents_info(torrent_hashes=job.qbt_hash)
                    assert len(matches) == 1
                    assert matches[0].save_path.rstrip("/") == str(root / "staging")
                    assert {"torrent_intake", job.unique_tag, "Review"} <= set(matches[0].tags.split(", "))
                    trackers = client.torrents_trackers(job.qbt_hash)
                    assert any("private-passkey" in tracker.url for tracker in trackers)
                duplicate_status, _ = send("/jobs/torrent", body, "multipart/form-data; boundary=test-boundary")
                assert duplicate_status == 409
                if b"pieces" in info:
                    status, _ = send("/jobs", json.dumps({**settings, "magnet_uri": parse_torrent(data).magnet_uri}).encode(), "application/json")
                    assert status == 409, "upload/magnet duplicates must not create a second job"
            with Session(engine) as db:
                assert db.query(Job).count() == 3
            print(f"PASS real qBittorrent {version}: v1/v2/hybrid multipart uploads, original bytes, tracker preservation, staging/tags, hash resolution and file/magnet duplicates")
        finally:
            if server:
                server.should_exit = True
            if thread:
                thread.join(timeout=15)
                assert not thread.is_alive(), "HTTP test server failed to stop"
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stdout.close()
            engine.dispose()


if __name__ == "__main__":
    main()
