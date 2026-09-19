import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from app import main
from app.admin import Controller
from app.config import Settings, persist_settings, saved_settings
from app.qbt import QbtService
from app.settings_editor import SettingsEditError, pending_settings, revision, validate_draft
from app.settings_view import build_settings_catalog


class SettingsEditorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {"TI_DATA_DIR": str(self.root)}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.settings = Settings(qbt_password="test-secret")
        persist_settings(self.settings)

    def test_draft_does_not_save_or_change_active_values(self):
        before = revision(self.settings)
        candidate, changes = validate_draft(self.settings, {"ui_title": "New title"})
        self.assertEqual(candidate.ui_title, "New title")
        self.assertEqual(self.settings.ui_title, "Torrent Intake")
        self.assertEqual(revision(self.settings), before)
        self.assertEqual(changes[0]["after"], "New title")

    def test_deployment_and_environment_fields_cannot_be_edited(self):
        for name in ("infected_action", "database_url", "data_dir", "local_staging_root", "ffprobe_binary", "managed_tag"):
            with self.subTest(name=name), self.assertRaises(SettingsEditError) as raised:
                validate_draft(self.settings, {name: "anything"})
            self.assertEqual(raised.exception.status, 409)
        with patch.dict(os.environ, {"TI_UI_TITLE": "From Portainer"}):
            with self.assertRaises(SettingsEditError) as raised:
                validate_draft(self.settings, {"ui_title": "Local"})
            self.assertIn("Remove TI_UI_TITLE", raised.exception.fields["ui_title"])

    def test_invalid_values_and_scanner_relationships(self):
        drafts = [
            {"qbt_host": "not a URL"}, {"qbt_host": "https://host:99999"},
            {"ui_title": " "}, {"polling_interval_seconds": -1},
            {"local_overflow_policy": "delete"}, {"per_job_scan_workers": 5},
            {"scanner_max_file_mib": 2001},
            {"scanner_definitions_warn_hours": 100},
            {"scan_heartbeat_seconds": 90}, {"max_scan_slots": 1},
            {"large_media_chunk_mib": 32, "large_media_min_chunk_mib": 64},
            {"per_job_scan_workers": 4, "clamd_max_inflight_requests": 2},
            {"qbt_password": ["never-expose-invalid-secret"]},
        ]
        for draft in drafts:
            with self.subTest(draft=list(draft)), self.assertRaises(SettingsEditError) as raised:
                validate_draft(self.settings, draft)
            self.assertTrue(raised.exception.fields)
            self.assertNotIn("never-expose", repr(raised.exception.fields))

    def test_secret_replacements_clear_and_redacted_review(self):
        candidate, changes = validate_draft(self.settings, {"qbt_password": ""})
        self.assertEqual(candidate.qbt_password, "test-secret")
        self.assertFalse(changes)
        candidate, changes = validate_draft(self.settings, {
            "qbt_password": "replacement-secret",
            "qbt_host": "https://user:private@host:8080?token=private",
            "completion_event_token": None,
        })
        self.assertEqual(candidate.qbt_password, "replacement-secret")
        self.assertIsNone(candidate.completion_event_token)
        self.assertNotIn("test-secret", repr(changes))
        self.assertNotIn("replacement-secret", repr(changes))
        self.assertNotIn("private", repr(changes))

    def test_catalog_permissions_pending_values_and_source(self):
        candidate, _ = validate_draft(self.settings, {"scanner_max_file_mib": 1000, "qbt_password": "replacement"})
        persist_settings(candidate)
        (self.root / "restart-required").touch()
        with patch.dict(os.environ, {"TI_DEBUG": "false"}):
            groups = build_settings_catalog(self.settings, pending_settings(self.settings))
        items = {item["name"]: item for group in groups for item in group["settings"]}
        self.assertEqual(items["debug"]["permission"], "environment")
        self.assertEqual(items["database_url"]["permission"], "deployment")
        self.assertEqual(items["scanner_max_file_mib"]["permission"], "advanced")
        self.assertEqual(items["scanner_max_file_mib"]["pending"], "1000")
        self.assertEqual(items["qbt_password"]["pending"], "Replacement saved (hidden)")
        self.assertIsNone(items["qbt_password"]["input_value"])
        self.assertEqual(items["ui_title"]["source"], "Saved locally")

    def test_connection_probe_is_independent_and_always_closes(self):
        probe = QbtService()
        probe.settings = self.settings
        shared = object()
        with patch.object(QbtService, "_shared_client", shared), patch("app.qbt.qbittorrentapi.Client") as factory:
            with patch.object(probe, "_log_in") as login:
                probe.test_connection()
                login.assert_called_once_with(factory.return_value)
            factory.return_value._session.close.assert_called_once()
            factory.return_value._session.close.reset_mock()
            with patch.object(probe, "_log_in", side_effect=RuntimeError("login failed")):
                with self.assertRaises(RuntimeError):
                    probe.test_connection()
            factory.return_value._session.close.assert_called_once()
            self.assertIs(QbtService._shared_client, shared)


class SettingsApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {"TI_DATA_DIR": str(self.root)}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.settings = Settings()
        persist_settings(self.settings)
        self.controller = Controller(self.settings, fresh=True)
        for name, value in (("settings", self.settings), ("controller", self.controller)):
            patcher = patch.object(main, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def call(self, handler, payload):
        with patch.object(main, "_admin_json", AsyncMock(return_value=payload)):
            return await handler(MagicMock())

    async def test_advanced_save_requires_confirmation_and_tracks_pending(self):
        payload = {"settings": {"scanner_max_file_mib": 1000}, "revision": revision(self.settings)}
        result = await self.call(main.review_local_settings, payload)
        self.assertTrue(result["changes"][0]["advanced"])
        self.assertFalse((self.root / "restart-required").exists())
        with self.assertRaises(HTTPException) as raised:
            await self.call(main.save_local_settings, payload)
        self.assertEqual(raised.exception.status_code, 409)
        await self.call(main.save_local_settings, {**payload, "confirm_advanced": True})
        self.assertEqual(saved_settings()["scanner_max_file_mib"], 1000)
        self.assertEqual(self.settings.scanner_max_file_mib, 2000)
        self.assertTrue(self.controller.status()["restart_required"])
        with self.assertRaises(HTTPException):
            await self.call(main.review_local_settings, payload)

    async def test_stale_revision_and_running_controller_cannot_save(self):
        with self.assertRaises(HTTPException) as raised:
            await self.call(main.save_local_settings, {"settings": {"ui_title": "New"}, "revision": "old"})
        self.assertEqual(raised.exception.status_code, 409)
        self.controller.paused = False
        with self.assertRaises(HTTPException) as raised:
            await self.call(main.save_local_settings, {"settings": {"ui_title": "New"}})
        self.assertIn("Pause", raised.exception.detail)
        self.assertEqual(saved_settings()["ui_title"], self.settings.ui_title)

    async def test_failed_write_keeps_controller_paused_and_requires_restart(self):
        with patch.object(main, "persist_settings", side_effect=OSError("private location")):
            with self.assertRaises(HTTPException) as raised:
                await self.call(main.save_local_settings, {"settings": {"ui_title": "New"}})
        self.assertNotIn("private location", raised.exception.detail)
        self.assertTrue(self.controller.status()["paused"])
        self.assertTrue(self.controller.status()["restart_required"])
        self.assertEqual(saved_settings()["ui_title"], self.settings.ui_title)

    async def test_failed_checks_prevent_resume_and_report_individual_results(self):
        checks = [{"name": "Test storage", "ok": False, "message": "Not mounted"}]
        with patch.object(main, "_readiness_checks", AsyncMock(return_value=checks)):
            with self.assertRaises(HTTPException) as raised:
                await self.call(main.resume_controller, {"confirm_external_state": True})
        self.assertEqual(raised.exception.detail["checks"], checks)
        self.assertTrue(self.controller.paused)

    async def test_connection_test_does_not_persist_draft(self):
        before = revision(self.settings)
        with patch.object(main, "_connection_check", AsyncMock(return_value={"ok": True})) as check:
            await self.call(main.test_qbt_connection, {"settings": {"qbt_host": "https://new-host", "qbt_request_timeout_seconds": 10}})
        candidate = check.call_args.args[0]
        self.assertEqual(candidate.qbt_host, "https://new-host")
        self.assertEqual(candidate.qbt_request_timeout_seconds, 10)
        self.assertEqual(revision(self.settings), before)

    async def test_storage_check_reports_permission_errors_without_crashing(self):
        with patch.object(main.Path, "is_dir", side_effect=PermissionError):
            self.assertTrue(all(not check["ok"] for check in main._storage_checks()))


if __name__ == "__main__":
    unittest.main()
