"""Real qB NAS selection -> clean promotion -> optional hook, entirely offline.

Run in the test-only qB image with --network none, /tmp:noexec and a writable
/hooks:exec and /copy-target:noexec tmpfs. ClamD itself is covered by the separate media integration;
this test supplies its clean checkpoint to isolate the real movement boundary.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import qbittorrentapi
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def wait_for(check, description: str, *, timeout: float = 20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError(f"Timed out waiting for {description}")


def main() -> None:
    assert os.getuid() != 0, "Run the integration image as its unprivileged application user"
    assert Path("/hooks").is_dir() and os.access("/hooks", os.W_OK | os.X_OK), "Mount writable /hooks:exec tmpfs"
    assert Path("/copy-target").is_dir(), "Mount writable /copy-target:noexec tmpfs"
    with tempfile.TemporaryDirectory(prefix="ti-locations-") as temporary, tempfile.TemporaryDirectory(
        prefix="ti-locations-hooks-", dir="/hooks",
    ) as hook_temporary, tempfile.TemporaryDirectory(prefix="intake-copies-", dir="/copy-target") as copy_temporary:
        root = Path(temporary)
        os.environ["TI_DATA_DIR"] = str(root / "app-data")
        os.environ["TI_EVENT_DIR"] = str(root / "events")
        from app.config import Settings, get_settings
        from app.db import Base
        from app.models import Job, ScanFile, ScanRun
        from app.post_promotion import PostPromotionRunner
        from app.qbt import QbtService
        from app.service import JobService
        from test_torrent_files import torrent, v1_info

        get_settings.cache_clear()
        config = root / "qBittorrent" / "config"
        config.mkdir(parents=True)
        # No network or non-loopback authentication changes leave this profile.
        (config / "qBittorrent.conf").write_text(
            "[LegalNotice]\nAccepted=true\n[Preferences]\n"
            "WebUI\\LocalHostAuth=false\nWebUI\\Address=127.0.0.1\n"
            "WebUI\\HostHeaderValidation=false\nConnection\\UPnP=false\n",
            encoding="utf-8",
        )
        process = subprocess.Popen(
            ["qbittorrent-nox", f"--profile={root}", "--webui-port=18084"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        engine = create_engine(f"sqlite:///{root / 'jobs.db'}", connect_args={"check_same_thread": False})
        try:
            client = qbittorrentapi.Client(host="http://127.0.0.1:18084", REQUESTS_ARGS={"timeout": 5})

            def qbt_started():
                assert process.poll() is None, process.stdout.read().decode()
                try:
                    return client.app_version()
                except qbittorrentapi.APIError:
                    return None

            version = wait_for(qbt_started, "qBittorrent startup")
            Base.metadata.create_all(engine)
            sessions = sessionmaker(bind=engine)
            library = root / "library"
            library.mkdir()
            locations = []
            for identifier in ("one", "two"):
                mount = root / f"nas-{identifier}"
                staging = mount / "staging"
                staging.mkdir(parents=True)
                marker = mount / ".mounted"
                marker.write_text(identifier, encoding="utf-8")
                locations.append({
                    "id": identifier, "label": f"NAS {identifier}", "path": str(staging),
                    "mount_marker": str(marker),
                })
            recorded = root / "hook-invocations.jsonl"
            script = Path(hook_temporary) / "verify-final.py"
            script.write_text(
                f"#!{sys.executable}\n"
                "import argparse, json\nfrom pathlib import Path\n"
                "p = argparse.ArgumentParser()\n"
                "for name in ('source', 'torrent-hash', 'torrent-name', 'job-id'): p.add_argument('--' + name, required=True)\n"
                "args = p.parse_args()\nsource = Path(args.source)\n"
                f"assert source.is_relative_to(Path({str(library)!r})), source\n"
                "assert source.read_bytes() == b'test data', source\n"
                f"with Path({str(recorded)!r}).open('a', encoding='utf-8') as handle: handle.write(json.dumps(vars(args)) + '\\n')\n"
                "print('Verified final payload:', source)\n",
                encoding="utf-8",
            )
            script.chmod(0o700)
            settings = Settings(
                _env_file=None, local_staging_root=str(root / "local"),
                nas_staging_locations=locations, default_nas_staging_id="one",
                final_parent_prefix=str(library), post_promotion_enabled=True,
                post_promotion_script=str(script), post_promotion_delay_seconds=0,
                post_promotion_timeout_seconds=10,
            )
            service = JobService()
            service.settings = settings
            service.qbt = QbtService.__new__(QbtService)
            service.qbt._with_client = lambda operation: operation(client)
            runner = PostPromotionRunner(settings=settings, qbt=service.qbt, session_factory=sessions)

            for location in locations:
                identifier = location["id"]
                if identifier == "two":
                    target = Path(copy_temporary)
                    (target / ".intake-copy-mount").touch()
                    settings = Settings(**{**settings.model_dump(), "post_promotion_enabled": False,
                                          "post_promotion_script": None, "post_promotion_copy_enabled": True,
                                          "post_promotion_copy_destination": str(target)})
                    service.settings = runner.settings = settings
                name = f"payload {identifier} $(ignored); 'quoted'.txt"
                staged_source = Path(location["path"]) / name
                final_parent = library / f"new-{identifier}" / "nested"
                assert not final_parent.exists()
                with sessions() as db:
                    job = service.submit_job(
                        db, torrent_file_data=torrent(v1_info(name=name.encode())),
                        torrent_file_name=f"{identifier}.torrent", final_parent=str(final_parent),
                        final_category=None, staging_preference="nas", nas_staging_id=identifier,
                    )
                    assert job.nas_staging_id == identifier
                    assert job.staging_root_initial == location["path"]
                    assert job.nas_mount_marker == location["mount_marker"]

                    def hash_resolved():
                        if not job.qbt_hash:
                            service._resolve_hash_for_job(db, job)
                        return job.qbt_hash

                    torrent_hash = wait_for(hash_resolved, f"{identifier} canonical hash")

                    original_parent = str(final_parent)
                    final_parent = library / f"edited-{identifier}" / "nested"
                    job = service.update_final_destination(
                        db, job_id=job.id, final_parent=str(final_parent),
                        expected_final_parent=original_parent,
                    )
                    assert job.final_parent == str(final_parent)
                    assert job.nas_staging_id == identifier and job.staging_root_actual == location["path"]
                    assert not final_parent.exists(), "Editing the plan must not create or move any content"
                    assert Path(service.qbt.get_torrent(torrent_hash).save_path) == Path(location["path"])

                    # Supply this offline torrent's bytes only after editing its
                    # destination while incomplete; let real qB verify them.
                    service.qbt.pause(torrent_hash)
                    wait_for(lambda: runner.guard.is_paused(service.qbt.get_torrent(torrent_hash)),
                             f"{identifier} incomplete torrent pause")
                    staged_source.write_bytes(b"test data")
                    client.torrents_recheck(torrent_hashes=torrent_hash)

                    def complete():
                        live = service.qbt.get_torrent(torrent_hash)
                        return live if live is not None and runner.guard.is_complete(live) else None

                    live = wait_for(complete, f"{identifier} local payload verification")
                    assert Path(live.save_path) == Path(location["path"])
                    assert Path(live.content_path) == staged_source
                    assert {job.managed_tag, job.unique_tag} <= runner.guard.tags(live)
                    service.qbt.pause(torrent_hash)

                    def paused_complete():
                        live = complete()
                        return live if live is not None and runner.guard.is_paused(live) else None

                    wait_for(paused_complete, f"{identifier} paused completed torrent")
                    job.state, job.is_terminal = "scan_clean", False
                    job.scan_completed_at = datetime.utcnow()
                    job.content_path = str(staged_source)
                    db.add(ScanRun(
                        job_id=job.id, verdict="clean", root_path=str(staged_source),
                        total_files=1, completed_files=1, total_bytes=9, completed_bytes=9,
                    ))
                    db.add(ScanFile(
                        job_id=job.id, relative_path=".", size_bytes=9,
                        mtime_ns=staged_source.stat().st_mtime_ns, status="clean",
                    ))
                    db.commit()
                    before = recorded.read_text().splitlines() if recorded.exists() else []
                    assert runner.claim_next() is None, "A clean scan alone must not execute a hook"
                    assert not service._reconcile_clean_promotion(db, job), "Requesting a qB move must not finalize promotion"
                    assert job.state == "promoting" and job.hook_status is None
                    assert runner.claim_next() is None, "A requested but unverified move must not execute a hook"

                    def promoted():
                        return service._reconcile_clean_promotion(db, job)

                    wait_for(promoted, f"{identifier} actual qB final move reconciliation")
                    db.refresh(job)
                    source = final_parent / name
                    assert job.state == "done" and job.is_terminal
                    assert job.hook_status == "pending"
                    assert job.content_path == str(source)
                    assert source.read_bytes() == b"test data"
                    assert not staged_source.exists()
                    assert (recorded.read_text().splitlines() if recorded.exists() else []) == before
                    claim = wait_for(runner.claim_next, f"{identifier} final hook readiness")
                    assert claim.job_id == job.id and claim.source == str(source)
                    asyncio.run(runner.run_claim(claim, asyncio.Event()))
                    db.refresh(job)
                    assert job.hook_status == "succeeded", (job.hook_error, job.hook_output)
                    assert job.hook_attempts == 1 and job.hook_exit_code == 0
                    if identifier == "one":
                        assert job.hook_kind == "script"
                        assert json.loads(recorded.read_text().splitlines()[-1]) == {
                            "source": str(source), "torrent_hash": torrent_hash,
                            "torrent_name": name, "job_id": job.id,
                        }
                    else:
                        assert job.hook_kind == "copy" and job.hook_script is None
                        assert job.hook_destination == str(target)
                        copy_folder = target / f"intake-job-{job.id}"
                        assert (copy_folder / name).read_bytes() == source.read_bytes()
                        assert (copy_folder / ".intake-copy-complete.json").is_file()
                        assert recorded.read_text().splitlines() == before, "Built-in copying must not execute an operator script"
                    assert runner.claim_next() is None, "Finished hooks must never replay automatically"
                    assert source.read_bytes() == b"test data", "The hook must retain the seeding source"
                    assert Path(service.qbt.get_torrent(torrent_hash).save_path) == final_parent

            invocations = [json.loads(line) for line in recorded.read_text().splitlines()]
            assert len(invocations) == 1
            print(
                f"PASS real qBittorrent {version}: two pinned NAS locations, complete/paused local payloads, "
                "edited download destinations used for real moves into absent final subdirectories, no early hooks, literal path/name arguments, "
                "exactly one script/copy per job, built-in rsync without script configuration, retained seeding originals"
            )
        finally:
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
