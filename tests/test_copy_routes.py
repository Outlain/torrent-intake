from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from app.config import PostPromotionCopyRule, Settings
from app.models import Job
from app.post_promotion import HookClaim, matching_copy_rule, queue_promotion_hook


def rule(source="/downloads/Movies", destination="/copy-target/Movies", enabled=True):
    return {"source": source, "destination": destination, "enabled": enabled}


class CopyRouteSettingsTests(unittest.TestCase):
    def settings(self, rules=None, **kwargs):
        return Settings(_env_file=None, post_promotion_copy_enabled=True,
                        post_promotion_copy_rules=rules if rules is not None else [rule()], **kwargs)

    def test_empty_rules_and_legacy_settings_load_without_enabling_implicit_fallback(self):
        for destination in (None, "/copy-target/old"):
            settings = self.settings([], post_promotion_copy_destination=destination)
            self.assertTrue(settings.post_promotion_copy_enabled)
            self.assertEqual(settings.post_promotion_copy_rules, [])

    def test_rule_is_typed_and_round_trips_without_losing_order_or_enabled_state(self):
        settings = self.settings([rule(), rule("/downloads/TV", "/copy-target", False)])
        self.assertIsInstance(settings.post_promotion_copy_rules[0], PostPromotionCopyRule)
        restored = Settings(_env_file=None, **settings.model_dump(mode="json"))
        self.assertEqual(restored.post_promotion_copy_rules, settings.post_promotion_copy_rules)

    def test_source_must_be_valid_final_storage_not_operational_or_staging(self):
        for source in ("relative", "/etc", "/downloads-other", "/downloads/../etc", "/downloads/Bad\nName",
                       "/downloads/docker/library", "/downloads/torrent-intake/staging", "/staging-local"):
            with self.subTest(source=source), self.assertRaises(ValidationError):
                self.settings([rule(source=source)])

    def test_destination_must_use_copy_mount(self):
        for destination in ("", "relative", "/downloads/Movies", "/copy-target-evil",
                            "/copy-target/../etc", "/copy-target/Bad\nName"):
            with self.subTest(destination=destination), self.assertRaises(ValidationError):
                self.settings([rule(destination=destination)])

    def test_custom_operational_source_directories_are_rejected(self):
        for setting in ("data_dir", "event_dir", "quarantine_root"):
            with self.subTest(setting=setting), self.assertRaisesRegex(ValidationError, "operational"):
                self.settings([rule(source="/downloads/PrivateState")], **{setting: "/downloads/PrivateState"})

    def test_duplicate_sources_rejected_even_if_disabled_or_spelled_differently(self):
        for second in (rule(enabled=False), rule(source="/downloads/Movies/"), rule(source="/downloads//Movies")):
            with self.subTest(second=second), self.assertRaisesRegex(ValidationError, "unique source"):
                self.settings([rule(), second])

    def test_symlink_source_cannot_escape_allowed_roots_or_create_duplicate_route(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            library = base / "library"
            library.mkdir()
            outside = base / "outside"
            outside.mkdir()
            (library / "escape").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValidationError):
                self.settings([rule(str(library / "escape"))], final_parent_prefix=str(library))
            (library / "same").symlink_to(library, target_is_directory=True)
            with self.assertRaisesRegex(ValidationError, "unique source"):
                self.settings([rule(str(library)), rule(str(library / "same"))], final_parent_prefix=str(library))

    def test_physical_alias_is_rejected_if_available(self):
        with patch("app.config.Path.samefile", return_value=True):
            with self.assertRaisesRegex(ValidationError, "aliases its source"):
                self.settings()

    def test_maximum_rule_count_and_unknown_fields(self):
        self.settings([rule(f"/downloads/{index}") for index in range(32)])
        with self.assertRaises(ValidationError):
            self.settings([rule(f"/downloads/{index}") for index in range(33)])
        with self.assertRaises(ValidationError):
            self.settings([{**rule(), "shell_command": "unwanted"}])

    def test_copy_and_script_modes_remain_mutually_exclusive(self):
        with self.assertRaises(ValidationError):
            self.settings(post_promotion_enabled=True, post_promotion_script="/hooks/after.sh")


class CopyRouteQueueTests(unittest.TestCase):
    def settings(self, rules):
        return Settings(_env_file=None, post_promotion_copy_enabled=True, post_promotion_copy_rules=rules)

    def job(self, final_parent="/downloads/Movies/Action", content_path=None):
        return Job(id="copy-job", final_parent=final_parent,
                   content_path=content_path or final_parent + "/Film",
                   state="done", is_terminal=True, promoted_at=datetime.utcnow(), scan_completed_at=datetime.utcnow())

    def test_longest_enabled_component_ancestor_wins_independently_of_order(self):
        broad = rule("/downloads", "/copy-target/All")
        narrow = rule("/downloads/Movies", "/copy-target/Movies")
        disabled = rule("/downloads/Movies/Action", "/copy-target/Disabled", False)
        for rules in ([broad, narrow, disabled], [disabled, narrow, broad]):
            with self.subTest(rules=rules):
                settings = self.settings(rules)
                match = matching_copy_rule("/downloads/Movies/Action", settings)
                self.assertEqual(match.destination, "/copy-target/Movies")

    def test_exact_source_match_and_nested_relative_path_are_pinned(self):
        settings = self.settings([rule()])
        for parent, relative in (("/downloads/Movies", "Film"), ("/downloads/Movies/Action", "Action/Film")):
            with self.subTest(parent=parent):
                job = self.job(parent)
                self.assertTrue(queue_promotion_hook(job, settings))
                self.assertEqual(job.hook_destination, "/copy-target/Movies")
                self.assertEqual(job.hook_copy_source_root, "/downloads/Movies")
                self.assertEqual(job.hook_copy_relative_path, relative)
                self.assertEqual(job.hook_status, "pending")

    def test_unmatched_or_disabled_rules_do_not_fall_back_to_legacy_global_target(self):
        for rules, parent in (([rule()], "/downloads/MoviesBackup"),
                              ([rule()], "/downloads/TV"),
                              ([rule(enabled=False)], "/downloads/Movies"),
                              ([], "/downloads/Movies")):
            with self.subTest(rules=rules, parent=parent):
                settings = self.settings(rules)
                settings.post_promotion_copy_destination = "/copy-target/legacy"
                job = self.job(parent)
                self.assertFalse(queue_promotion_hook(job, settings))
                self.assertIsNone(job.hook_status)
                self.assertIsNone(job.hook_destination)

    def test_master_disabled_skips_copy_despite_matching_rule(self):
        settings = self.settings([rule()])
        settings.post_promotion_copy_enabled = False
        self.assertFalse(queue_promotion_hook(self.job(), settings))

    def test_queue_rejects_content_outside_source_and_traversal(self):
        settings = self.settings([rule()])
        for content in ("/downloads/Movies", "/downloads/TV/Film", "/downloads/Movies/../TV/Film", "relative"):
            with self.subTest(content=content):
                self.assertFalse(queue_promotion_hook(self.job(content_path=content), settings))

    def test_explicit_retry_keeps_pinned_route_even_after_rule_removed(self):
        settings = self.settings([rule()])
        job = self.job()
        self.assertTrue(queue_promotion_hook(job, settings))
        settings.post_promotion_copy_rules = []
        job.hook_status = None
        job.hook_attempts = 2
        self.assertTrue(queue_promotion_hook(job, settings))
        self.assertEqual(job.hook_destination, "/copy-target/Movies")
        self.assertEqual(job.hook_copy_source_root, "/downloads/Movies")
        self.assertEqual(job.hook_copy_relative_path, "Action/Film")
        self.assertEqual(job.hook_attempts, 2)

    def test_mapped_claim_uses_explicit_arguments_not_shell_expansion(self):
        claim = HookClaim("id", None, "/downloads/Movies/Film", "hash", "Title", 1, "copy",
                          "/copy-target/Movies", "/downloads/Movies", "Film")
        self.assertEqual(claim.argv[5:8], ["--source-root", "/downloads/Movies", "--relative-path=Film"])
        dash = HookClaim("id", None, "/downloads/Movies/-Title/file.mkv", "hash", "Title", 1, "copy",
                         "/copy-target/Movies", "/downloads/Movies", "-Title/file.mkv")
        self.assertIn("--relative-path=-Title/file.mkv", dash.argv)
        broken = HookClaim("id", None, "/downloads/Movies/Film", "hash", "Title", 1, "copy",
                           "/copy-target/Movies", "/downloads/Movies", None)
        with self.assertRaisesRegex(RuntimeError, "incomplete pinned routing"):
            _ = broken.argv


class CopyRouteReadinessTests(unittest.TestCase):
    def test_readiness_checks_only_distinct_enabled_rule_destinations_and_never_blocks_intake(self):
        from app import main
        settings = Settings(_env_file=None, post_promotion_copy_enabled=True,
                            post_promotion_copy_destination="/copy-target/legacy",
                            post_promotion_copy_rules=[rule(), rule("/downloads/TV", "/copy-target/Movies"),
                                                       rule("/downloads/ISO", "/copy-target/Disabled", False)])
        with patch("app.main.settings", settings), patch("app.main.validate_copy_destination", side_effect=OSError("offline")) as validate:
            checks = [item for item in main._storage_checks() if item["name"].startswith("Copy")]
        validate.assert_called_once_with(Path("/copy-target/Movies"))
        self.assertEqual(len(checks), 1)
        self.assertFalse(checks[0]["ok"])
        self.assertFalse(checks[0]["required"])

    def test_empty_rules_produce_optional_migration_warning(self):
        from app import main
        settings = Settings(_env_file=None, post_promotion_copy_enabled=True,
                            post_promotion_copy_destination="/copy-target/legacy")
        with patch("app.main.settings", settings), patch("app.main.validate_copy_destination") as validate:
            checks = [item for item in main._storage_checks() if item["name"].startswith("Copy")]
        validate.assert_not_called()
        self.assertEqual(checks[0]["name"], "Copy rules")
        self.assertFalse(checks[0]["required"])


if __name__ == "__main__":
    unittest.main()
