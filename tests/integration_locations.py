"""Real qB NAS selection -> paused destination edit -> promotion/hook, offline.

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
import threading
import time
from unittest.mock import patch

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
        from app.post_promotion import PostPromotionRunner, matching_copy_rule
        from app.qbt import QbtService
        from app.scanner import ScannerHealth, ScannerIdentity
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
            coordinator = service.scan_coordinator
            coordinator.settings = settings
            coordinator.qbt = service.qbt
            runner = PostPromotionRunner(settings=settings, qbt=service.qbt, session_factory=sessions)
            # ClamD is deliberately outside this movement integration. Seed its
            # clean checkpoint and provide the same identity when resuming it.
            identity = ScannerIdentity(
                backend="clamd", engine_version="1.4.3", database_version="12345",
                database_updated_at=datetime.utcnow(), policy_version="location-integration-v1",
                raw_version="ClamAV 1.4.3/12345",
            )

            for number, location in enumerate([locations[0], locations[1], locations[1]]):
                identifier = ("one", "two", "unmatched")[number]
                if identifier == "two":
                    target = Path(copy_temporary)
                    (target / ".intake-copy-mount").touch()
                    settings = Settings(**{**settings.model_dump(), "post_promotion_enabled": False,
                                          "post_promotion_script": None, "post_promotion_copy_enabled": True,
                                          "post_promotion_copy_rules": [{
                                              "source": str(library / "paused-edited-two"),
                                              "destination": str(target), "enabled": True,
                                          }]})
                    service.settings = runner.settings = coordinator.settings = settings
                name = f"payload {identifier} $(ignored); 'quoted'.txt"
                staged_source = Path(location["path"]) / name
                final_parent = library / f"new-{identifier}" / "nested"
                assert not final_parent.exists()
                with sessions() as db:
                    job = service.submit_job(
                        db, torrent_file_data=torrent(v1_info(name=name.encode())),
                        torrent_file_name=f"{identifier}.torrent", final_parent=str(final_parent),
                        final_category=None, staging_preference="nas", nas_staging_id=location["id"],
                    )
                    assert job.nas_staging_id == location["id"]
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
                    assert job.nas_staging_id == location["id"] and job.staging_root_actual == location["path"]
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
                    job.state, job.is_terminal = ("scan_paused" if identifier == "two" else "scan_clean"), False
                    job.completion_event_received_at = job.download_complete_at = datetime.utcnow()
                    job.scan_completed_at = None if identifier == "two" else datetime.utcnow()
                    job.content_path = str(staged_source)
                    run = ScanRun(
                        job_id=job.id, pause_requested=identifier == "two",
                        verdict=None if identifier == "two" else "clean", root_path=str(staged_source),
                        total_files=1, completed_files=1, total_bytes=9, completed_bytes=9,
                    )
                    coordinator._sync_run_identity(run, identity)
                    fingerprint = staged_source.stat()
                    checkpoint = ScanFile(
                        job_id=job.id, relative_path=".", size_bytes=9,
                        mtime_ns=fingerprint.st_mtime_ns, ctime_ns=fingerprint.st_ctime_ns,
                        device=fingerprint.st_dev, inode=fingerprint.st_ino, status="clean",
                        attempts=1, scanned_at=datetime.utcnow(), scanner_version=identity.raw_version,
                        engine_version=identity.engine_version, database_version=identity.database_version,
                        database_updated_at=identity.database_updated_at, policy_version=identity.policy_version,
                        scan_method="integration-supplied-clean-checkpoint",
                    )
                    db.add_all([run, checkpoint])
                    db.commit()
                    if identifier == "two":
                        # Model a pause after the final clean file: progress is
                        # 100%, but the final manifest gate has not run yet.
                        original_paused_parent = final_parent
                        checkpoint_before = {column.name: getattr(checkpoint, column.name)
                                             for column in ScanFile.__table__.columns}
                        run_before = {column.name: getattr(run, column.name)
                                      for column in ScanRun.__table__.columns}
                        assert matching_copy_rule(str(original_paused_parent), settings) is None
                        final_parent = library / "paused-edited-two" / "nested"
                        job = service.update_final_destination(
                            db, job_id=job.id, final_parent=str(final_parent),
                            expected_final_parent=str(original_paused_parent),
                        )
                        db.refresh(run)
                        db.refresh(checkpoint)
                        assert job.state == "scan_paused" and job.scan_completed_at is None
                        assert job.content_path == str(staged_source) and job.final_category is None
                        assert job.nas_staging_id == location["id"] and job.staging_root_actual == location["path"]
                        assert {column.name: getattr(checkpoint, column.name)
                                for column in ScanFile.__table__.columns} == checkpoint_before
                        assert {column.name: getattr(run, column.name)
                                for column in ScanRun.__table__.columns} == run_before
                        assert not final_parent.exists() and not original_paused_parent.exists()
                        assert staged_source.read_bytes() == b"test data"
                        assert Path(service.qbt.get_torrent(torrent_hash).save_path) == Path(location["path"])
                        assert runner.claim_next() is None, "Editing a paused plan must not queue a hook"

                        coordinator._resume_job(db, job.id)
                        assert job.state == "scan_pending" and not run.pause_requested
                        health = ScannerHealth(
                            status="healthy", can_scan=True, message="Integration-supplied checkpoint",
                            checked_at=datetime.utcnow(), identity=identity,
                        )
                        with patch("app.scan_coordinator.SessionLocal", sessions), \
                                patch.object(coordinator.scanner, "health", return_value=health), \
                                patch.object(coordinator.scanner, "require_healthy", return_value=identity), \
                                patch.object(coordinator.scanner, "scan_path", side_effect=AssertionError(
                                    "An unchanged clean checkpoint must not be rescanned"
                                )) as scan_path:
                            claims = coordinator.claim_jobs(db, "integration-paused-destination")
                            assert len(claims) == 1 and claims[0].job_id == job.id
                            coordinator.run_claim(claims[0], threading.Event())
                            scan_path.assert_not_called()
                        db.refresh(job)
                        db.refresh(run)
                        db.refresh(checkpoint)
                        assert job.state == "scan_clean" and job.scan_completed_at is not None, job.last_error
                        assert job.final_parent == str(final_parent) and run.verdict == "clean"
                        assert run.worker_id is None and run.lease_expires_at is None
                        assert {column.name: getattr(checkpoint, column.name)
                                for column in ScanFile.__table__.columns} == checkpoint_before
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
                    assert job.hook_status == (None if identifier == "unmatched" else "pending")
                    assert job.content_path == str(source)
                    assert source.read_bytes() == b"test data"
                    assert not staged_source.exists()
                    assert (recorded.read_text().splitlines() if recorded.exists() else []) == before
                    if identifier == "unmatched":
                        assert runner.claim_next() is None, "Unmatched final locations must not be copied"
                        assert not (target / name).exists()
                        assert not (target / "nested" / name).exists()
                        assert source.read_bytes() == b"test data"
                        continue
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
                        assert job.hook_copy_source_root == str(library / "paused-edited-two")
                        assert job.hook_copy_relative_path == str(Path("nested") / name)
                        assert not original_paused_parent.exists(), "Promotion must not use the pre-pause destination"
                        assert (target / "nested" / name).read_bytes() == source.read_bytes()
                        assert not (target / f"intake-job-{job.id}").exists(), "Mapped copies must not add a job wrapper"
                        assert recorded.read_text().splitlines() == before, "Built-in copying must not execute an operator script"
                    assert runner.claim_next() is None, "Finished hooks must never replay automatically"
                    assert source.read_bytes() == b"test data", "The hook must retain the seeding source"
                    assert Path(service.qbt.get_torrent(torrent_hash).save_path) == final_parent
                    if identifier == "two":
                        # Keep completed history and its receipt while deleting
                        # only the real qB entry, then re-add the same torrent.
                        copied = target / "nested" / name
                        copied_before = copied.stat()
                        old_receipt = target / ".intake-copy-state" / job.id / "complete.json"
                        receipt_before = old_receipt.read_bytes()
                        client.torrents_delete(torrent_hashes=torrent_hash, delete_files=False)
                        wait_for(lambda: service.qbt.get_torrent(torrent_hash) is None,
                                 "removed qB torrent before re-add")
                        assert source.read_bytes() == copied.read_bytes() == b"test data"
                        assert job.state == "done" and job.hook_status == "succeeded"
                        # Reuse the disposable seed without downloading from
                        # peers or leaving a preexisting qB promotion target.
                        source.rename(staged_source)
                        new_job = service.submit_job(
                            db, torrent_file_data=torrent(v1_info(name=name.encode())),
                            torrent_file_name="readded-two.torrent", final_parent=str(final_parent),
                            final_category=None, staging_preference="nas", nas_staging_id=location["id"],
                        )
                        assert new_job.id != job.id and new_job.unique_tag != job.unique_tag

                        def readded_hash():
                            if not new_job.qbt_hash:
                                service._resolve_hash_for_job(db, new_job)
                            return new_job.qbt_hash

                        assert wait_for(readded_hash, "re-added canonical hash") == torrent_hash
                        service.qbt.pause(torrent_hash)
                        wait_for(lambda: runner.guard.is_paused(service.qbt.get_torrent(torrent_hash)),
                                 "re-added torrent pause before local recheck")
                        client.torrents_recheck(torrent_hashes=torrent_hash)
                        try:
                            live = wait_for(complete, "re-added local payload verification")
                        except AssertionError as exc:
                            live = service.qbt.get_torrent(torrent_hash)
                            raise AssertionError(f"{exc}; state={live.state}, progress={live.progress}, "
                                                 f"content_path={live.content_path}, staging_exists={staged_source.exists()}") from exc
                        assert new_job.unique_tag in runner.guard.tags(live)
                        assert job.unique_tag not in runner.guard.tags(live)
                        callback_job = service.ingest_completion_event(
                            db, qbt_hash=torrent_hash, qbt_hash_v2=None,
                            unique_tag=new_job.unique_tag, tags=live.tags, torrent_name=name,
                            content_path=str(staged_source), root_path=None,
                            save_path=location["path"], size_bytes=9,
                        )
                        assert callback_job.id == new_job.id, "Old done history must not steal the new completion callback"
                        assert new_job.state == "completion_event_received"
                        service.qbt.pause(torrent_hash)
                        wait_for(paused_complete, "re-added paused completed torrent")
                        new_job.state, new_job.is_terminal = "scan_clean", False
                        new_job.download_complete_at = new_job.scan_completed_at = datetime.utcnow()
                        fingerprint = staged_source.stat()
                        db.add(ScanFile(
                            job_id=new_job.id, relative_path=".", size_bytes=9,
                            mtime_ns=fingerprint.st_mtime_ns, ctime_ns=fingerprint.st_ctime_ns,
                            device=fingerprint.st_dev, inode=fingerprint.st_ino, status="clean",
                            attempts=1, scanned_at=datetime.utcnow(), scanner_version=identity.raw_version,
                            engine_version=identity.engine_version, database_version=identity.database_version,
                            database_updated_at=identity.database_updated_at, policy_version=identity.policy_version,
                            scan_method="integration-supplied-clean-checkpoint",
                        ))
                        db.commit()
                        wait_for(lambda: service._reconcile_clean_promotion(db, new_job),
                                 "re-added real qB final move reconciliation")
                        db.refresh(new_job)
                        assert new_job.state == "done" and new_job.hook_status == "pending"
                        claim = wait_for(runner.claim_next, "re-added copy readiness")
                        assert claim.job_id == new_job.id and claim.torrent_hash == torrent_hash
                        asyncio.run(runner.run_claim(claim, asyncio.Event()))
                        db.refresh(new_job)
                        db.refresh(job)
                        assert new_job.hook_status == "succeeded", (new_job.hook_error, new_job.hook_output)
                        assert "byte-for-byte identical" in new_job.hook_output
                        copied_after = copied.stat()
                        for attribute in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"):
                            assert getattr(copied_after, attribute) == getattr(copied_before, attribute), attribute
                        assert copied.read_bytes() == source.read_bytes() == b"test data"
                        assert old_receipt.read_bytes() == receipt_before, "Re-add must preserve the previous copy receipt"
                        assert (target / ".intake-copy-state" / new_job.id / "complete.json").is_file()
                        assert job.hook_status == "succeeded" and job.hook_attempts == 1
                        assert new_job.hook_attempts == 1 and runner.claim_next() is None
                        assert Path(service.qbt.get_torrent(torrent_hash).save_path) == final_parent

            invocations = [json.loads(line) for line in recorded.read_text().splitlines()]
            assert len(invocations) == 1
            print(
                f"PASS real qBittorrent {version}: two pinned NAS locations, complete/paused local payloads, "
                "download and paused destination edits used for real moves into absent final subdirectories, "
                "checkpoint-preserving resume with the final manifest gate, no early hooks, literal path/name arguments, "
                "one script and one routed copy with preserved nested paths, unmatched final location skipped, "
                "real qB delete/re-add routes its callback to the new job and reuses byte-identical copied data "
                "without overwriting files or historical receipts, retained seeding originals"
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
