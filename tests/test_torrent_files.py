from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.config import Settings
from app.db import Base
from app.metainfo import parse_torrent
from app.models import Job
from app.qbt import QbtService, TorrentAlreadyExistsError
from app.schemas import JobOut
from app.service import JobService
from app.uploads import read_torrent_upload


def bencode(value):
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, list):
        return b"l" + b"".join(map(bencode, value)) + b"e"
    return b"d" + b"".join(bencode(key) + bencode(value[key]) for key in sorted(value)) + b"e"


def v1_info(**changes):
    result = {b"name": b"example.txt", b"length": 9, b"piece length": 16384,
              b"pieces": hashlib.sha1(b"test data").digest(), b"private": 1}
    result.update({key.encode(): value for key, value in changes.items()})
    return result


def torrent(info=None):
    return bencode({b"announce": b"http://127.0.0.1:9/private-passkey/announce",
                    b"announce-list": [[b"http://127.0.0.1:9/private-passkey/announce"]],
                    b"info": v1_info() if info is None else info})


def v2_info():
    return {b"name": b"example.txt", b"meta version": 2, b"piece length": 16384,
            b"file tree": {b"example.txt": {b"": {b"length": 9,
                b"pieces root": hashlib.sha256(b"test data").digest()}}}}


class MetainfoTests(unittest.TestCase):
    def test_original_private_metadata_and_exact_info_hash(self):
        data = torrent()
        parsed = parse_torrent(data)
        self.assertIs(parsed.data, data)
        self.assertEqual(parsed.size_bytes, 9)
        self.assertEqual(parsed.name, "example.txt")
        self.assertEqual(parsed.magnet_uri, "magnet:?xt=urn:btih:" + hashlib.sha1(bencode(v1_info())).hexdigest())
        self.assertNotIn("passkey", parsed.magnet_uri)

    def test_multifile(self):
        info = v1_info()
        del info[b"length"]
        info[b"files"] = [{b"path": [b"season", b"episode.mkv"], b"length": 5},
                          {b"path": [b"readme.txt"], b"length": 4}]
        self.assertEqual(parse_torrent(torrent(info)).size_bytes, 9)

    def test_v2_and_hybrid(self):
        for info in (v2_info(), {**v1_info(), **v2_info()}):
            with self.subTest(hybrid=b"pieces" in info):
                parsed = parse_torrent(torrent(info))
                self.assertEqual(parsed.size_bytes, 9)
                self.assertIn("xt=urn:btmh:1220" + hashlib.sha256(bencode(info)).hexdigest(), parsed.magnet_uri)
                self.assertEqual("urn:btih:" in parsed.magnet_uri, b"pieces" in info)

    def test_rejects_malformed_or_truncated_metadata(self):
        for data in (b"", b"not a torrent", b"le", b"de", torrent()[:-1], torrent() + b"extra",
                     b"d1:ai01ee", b"d1:ai-0ee", b"d1:ai1e1:ai2ee", b"d1:zi1e1:ai2ee"):
            with self.subTest(data=data[:30]), self.assertRaises(ValueError):
                parse_torrent(data)

    def test_metadata_resource_budgets(self):
        for setting, value in (("MAX_TORRENT_BYTES", 10), ("MAX_METAINFO_NODES", 4), ("MAX_METAINFO_DEPTH", 1)):
            with self.subTest(setting=setting), patch("app.metainfo." + setting, value), self.assertRaises(ValueError):
                parse_torrent(torrent())

    def test_unsafe_paths_and_symlinks_are_rejected(self):
        for name in (b"..", b"/outside", b"C:drive", b"dir/file", b"dir\\file", b"a\0b"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "unsafe"):
                parse_torrent(torrent(v1_info(name=name)))
        for changes in ({"attr": b"l"}, {"symlink path": [b"outside"]}, {"length": -1},
                        {"pieces": b"wrong"}, {"name.utf-8": b"../escape"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                parse_torrent(torrent(v1_info(**changes)))

    def test_multifile_duplicate_overlap_and_alternate_traversal(self):
        for second in ([b"a"], [b"a", b"child"]):
            info = v1_info()
            del info[b"length"]
            info[b"files"] = [{b"path": [b"a"], b"length": 4}, {b"path": second, b"length": 5}]
            with self.assertRaises(ValueError):
                parse_torrent(torrent(info))
        info[b"files"][1] = {b"path": [b"safe"], b"path.utf-8": [b"..", b"escape"], b"length": 5}
        with self.assertRaises(ValueError):
            parse_torrent(torrent(info))

    def test_v2_invalid_tree(self):
        for tree in ({}, {b"..": {b"": {b"length": 0}}}, {b"a": {}},
                     {b"a": {b"": {b"length": 9, b"pieces root": b"short"}}}):
            info = v2_info()
            info[b"file tree"] = tree
            with self.subTest(tree=tree), self.assertRaises(ValueError):
                parse_torrent(torrent(info))


class TorrentServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.service = JobService.__new__(JobService)
        self.service.settings = Settings(_env_file=None)
        self.service.qbt = MagicMock()
        self.service.qbt.find_existing_from_magnet.return_value = None
        self.service.qbt.list_torrents.return_value = []
        self.service.logger = logging.getLogger(__name__)
        self.service.scan_coordinator = MagicMock()
        self.service._resolve_hash_for_job = MagicMock()
        self.service._evaluate_staging_now = MagicMock()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def submit(self, **changes):
        options = dict(torrent_file_data=torrent(), torrent_file_name="example.torrent",
                       final_parent="/downloads/Shows", final_category="Shows",
                       staging_preference="local", custom_tags=["Review"])
        return self.service.submit_job(self.db, **{**options, **changes})

    def test_original_bytes_use_same_staging_category_and_tags(self):
        for preference, root in (("local", "/staging-local"), ("nas", "/downloads/torrent-intake/staging")):
            job = self.submit(staging_preference=preference)
            call = self.service.qbt.add_torrent.call_args.kwargs
            self.assertEqual(call["torrent_file_data"], torrent())
            self.assertEqual(call["save_path"], root)
            self.assertEqual(call["category"], self.service.settings.intake_category)
            self.assertEqual(call["tags"], [self.service.settings.managed_tag, job.unique_tag, "Review"])
            self.service._evaluate_staging_now.assert_called_with(self.db, job)

    def test_metadata_survives_restart_retry_and_is_not_in_job_responses(self):
        job = self.submit()
        identifier = job.id
        job.state = "error"
        self.db.commit()
        self.db.close()
        self.db = Session(self.engine)
        job = self.db.get(Job, identifier)
        self.assertIn("torrent_file_data", inspect(job).unloaded)
        output = JobOut.model_validate(job).model_dump()
        self.assertEqual(output["torrent_file_name"], "example.torrent")
        self.assertNotIn("torrent_file_data", output)
        self.assertIn("torrent_file_data", inspect(job).unloaded)
        self.service._find_live_torrent_for_job = MagicMock(return_value=None)
        self.service.qbt.add_torrent.reset_mock()
        self.service.retry_job(self.db, job_id=identifier)
        self.assertEqual(self.service.qbt.add_torrent.call_args.kwargs["torrent_file_data"], torrent())

    def test_failed_add_retains_metadata_for_retry(self):
        self.service.qbt.add_torrent.side_effect = RuntimeError("offline")
        with self.assertRaises(RuntimeError):
            self.submit()
        job = self.db.query(Job).one()
        self.assertEqual(job.state, "error")
        self.assertEqual(job.torrent_file_data, torrent())

    def test_existing_torrent_and_malformed_upload_do_not_create_jobs(self):
        self.service.qbt.find_existing_from_magnet.return_value = SimpleNamespace(name="existing", hash="a" * 40)
        with self.assertRaises(ValueError):
            self.submit()
        self.assertEqual(self.db.query(Job).count(), 0)
        self.service.qbt.add_torrent.assert_not_called()
        self.service.qbt.find_existing_from_magnet.reset_mock()
        with self.assertRaises(ValueError):
            self.submit(torrent_file_data=b"invalid")
        self.service.qbt.find_existing_from_magnet.assert_not_called()

    def test_unsafe_destination_and_conflicting_sources_rejected(self):
        for changes in ({"final_parent": "/etc"}, {"magnet_uri": "magnet:?xt=urn:btih:" + "a" * 40}):
            with self.assertRaises(ValueError):
                self.submit(**changes)
        self.service.qbt.add_torrent.assert_not_called()

    def test_saved_hash_mismatch_fails_closed(self):
        job = self.submit(torrent_file_name="../../example.torrent")
        self.assertEqual(job.torrent_file_name, "example.torrent")
        job.torrent_file_data = torrent(v1_info(name=b"different.txt"))
        self.service.qbt.add_torrent.reset_mock()
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.service._add_job_to_qbt(job, "/staging-local")
        self.service.qbt.add_torrent.assert_not_called()


class TorrentQbtTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.client.torrents_info.return_value = []
        self.client.torrents_add.return_value = "Ok."
        self.service = QbtService.__new__(QbtService)
        self.service._with_client = lambda operation: operation(self.client)

    def test_upload_sends_bytes_not_magnet(self):
        data = torrent()
        self.service.add_torrent(parse_torrent(data).magnet_uri, "/staging-local", ["Review"], "intake", torrent_file_data=data)
        options = self.client.torrents_add.call_args.kwargs
        self.assertEqual(options["torrent_files"], data)
        self.assertNotIn("urls", options)
        self.assertFalse(options["is_paused"])

    def test_magnet_route_unchanged(self):
        magnet = parse_torrent(torrent()).magnet_uri
        self.service.add_torrent(magnet, "/staging-local", [], "intake")
        self.assertEqual(self.client.torrents_add.call_args.kwargs["urls"], magnet)
        self.assertNotIn("torrent_files", self.client.torrents_add.call_args.kwargs)

    def test_file_magnet_duplicate_and_v2_hash_lookup(self):
        for info in (v1_info(), v2_info(), {**v1_info(), **v2_info()}):
            metadata = parse_torrent(torrent(info))
            self.client.torrents_info.return_value = [SimpleNamespace(hash="a" * 40, name="existing", save_path="/other")]
            with self.assertRaises(TorrentAlreadyExistsError):
                self.service.add_torrent(metadata.magnet_uri, "/staging-local", [], "intake", torrent_file_data=metadata.data)
            if b"meta version" in info:
                digest = hashlib.sha256(bencode(info)).hexdigest()
                hashes = self.client.torrents_info.call_args.kwargs["torrent_hashes"].split("|")
                self.assertIn(digest[:40], hashes)
                self.assertIn(digest, hashes)
        self.client.torrents_add.assert_not_called()


def multipart(parts=None):
    if parts is None:
        parts = [("settings", None, json.dumps({"final_parent": "/downloads/Shows", "custom_tags": ["Review"]}).encode()),
                 ("file", "example.torrent", torrent())]
    body = b""
    for name, filename, data in parts:
        header = f'Content-Disposition: form-data; name="{name}"'
        if filename is not None:
            header += f'; filename="{filename}"'
        body += b"--test-boundary\r\n" + header.encode() + b"\r\n\r\n" + data + b"\r\n"
    return body + b"--test-boundary--\r\n"


def upload_request(body, content_type="multipart/form-data; boundary=test-boundary", disconnect=False):
    messages = [{"type": "http.request", "body": body, "more_body": disconnect}]
    if disconnect:
        messages.append({"type": "http.disconnect"})

    async def receive():
        return messages.pop(0)
    return Request({"type": "http", "method": "POST", "path": "/jobs/torrent",
                    "headers": [(b"content-type", content_type.encode())]}, receive)


class TorrentUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_multipart_passes_settings_filename_and_exact_bytes(self):
        options, name, data = await read_torrent_upload(upload_request(multipart()))
        self.assertEqual(options.final_parent, "/downloads/Shows")
        self.assertEqual(options.custom_tags, ["Review"])
        self.assertEqual(name, "example.torrent")
        self.assertEqual(data, torrent())

    async def test_rejects_wrong_type_fields_filename_and_settings(self):
        settings = ("settings", None, b'{"final_parent":"/downloads/Shows"}')
        file = ("file", "example.torrent", torrent())
        for parts in ([file], [settings], [settings, file, file], [settings, settings, file],
                      [settings, ("file", "other.txt", torrent())],
                      [("settings", None, b"invalid json"), file],
                      [("settings", None, b'{"final_parent":"/etc"}'), file]):
            with self.subTest(parts=[item[:2] for item in parts]), self.assertRaises(HTTPException):
                await read_torrent_upload(upload_request(multipart(parts)))
        with self.assertRaises(HTTPException) as caught:
            await read_torrent_upload(upload_request(b"{}", "application/json"))
        self.assertEqual(caught.exception.status_code, 415)

    async def test_body_and_file_size_caps_without_content_length(self):
        for setting in ("MAX_UPLOAD_BYTES", "MAX_TORRENT_BYTES"):
            with patch("app.uploads." + setting, 64), self.assertRaises(HTTPException) as caught:
                await read_torrent_upload(upload_request(multipart()))
            self.assertEqual(caught.exception.status_code, 413)

    async def test_spooled_files_close_on_success_truncation_and_disconnect(self):
        original = tempfile.SpooledTemporaryFile
        for mode in ("success", "truncated", "disconnect"):
            files = []

            def spool(*args, **kwargs):
                handle = original(*args, **kwargs)
                files.append(handle)
                return handle

            body = multipart()
            with patch("starlette.formparsers.SpooledTemporaryFile", spool):
                if mode == "success":
                    await read_torrent_upload(upload_request(body))
                else:
                    with self.assertRaises(HTTPException):
                        await read_torrent_upload(upload_request(body[:-30], disconnect=mode == "disconnect"))
            self.assertTrue(files)
            self.assertTrue(all(handle.closed for handle in files), mode)

    async def test_route_forwards_to_common_service_without_blocking_event_loop(self):
        from app.main import create_job_from_torrent
        with patch("app.main.service") as service:
            service.submit_job.return_value = "created"
            result = await create_job_from_torrent(upload_request(multipart()), MagicMock())
        self.assertEqual(result, "created")
        self.assertEqual(service.submit_job.call_args.kwargs["torrent_file_data"], torrent())
        self.assertEqual(service.submit_job.call_args.kwargs["custom_tags"], ["Review"])


if __name__ == "__main__":
    unittest.main()
