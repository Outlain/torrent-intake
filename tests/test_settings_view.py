from pathlib import Path
import unittest

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import Settings
from app.settings_view import SETTING_SPECS, build_settings_catalog
from app.tags import MAX_CUSTOM_TAG_LENGTH, MAX_CUSTOM_TAGS, PRIVATE_JOB_TAG_PREFIX


ROOT = Path(__file__).resolve().parents[1]


def catalog_items(catalog: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {
        item["name"]: item
        for group in catalog
        for item in group["settings"]
    }


class SettingsViewTests(unittest.TestCase):
    def test_catalog_covers_every_settings_field(self) -> None:
        self.assertEqual(set(Settings.model_fields), set(SETTING_SPECS))

    def test_secrets_and_url_credentials_are_redacted(self) -> None:
        settings = Settings(
            qbt_password="qbt-super-secret",
            completion_event_token="completion-super-secret",
            qbt_host="https://api-user:api-secret@gluetun:8443/api?token=hidden",
            database_url="postgresql://db-user:db-secret@database:5432/intake?sslmode=require",
        )

        rendered = repr(build_settings_catalog(settings))
        self.assertNotIn("qbt-super-secret", rendered)
        self.assertNotIn("completion-super-secret", rendered)
        self.assertNotIn("api-secret", rendered)
        self.assertNotIn("db-secret", rendered)
        self.assertIn("Configured (hidden)", rendered)
        self.assertIn("https://hidden@gluetun:8443/api", rendered)
        self.assertIn("postgresql://hidden@database:5432/intake", rendered)

    def test_infected_action_is_visible_explained_and_read_only(self) -> None:
        items = catalog_items(build_settings_catalog(Settings(infected_action="delete")))
        infected = items["infected_action"]

        self.assertEqual(infected["current"], "delete")
        self.assertEqual(infected["default"], "hold")
        self.assertTrue(infected["safety_critical"])
        self.assertIn("remove the torrent and delete its data", infected["current_effect"])
        self.assertIn("Compose or Portainer", infected["change_hint"])

    def test_template_renders_settings_panel_without_secret_values(self) -> None:
        settings = Settings(
            infected_action="delete",
            qbt_password="never-render-this-password",
            completion_event_token="never-render-this-token",
        )
        environment = Environment(
            loader=FileSystemLoader(ROOT / "templates"),
            autoescape=select_autoescape(("html",)),
        )
        html = environment.get_template("index.html").render(
            title=settings.ui_title,
            jobs=[],
            settings=settings,
            settings_catalog=build_settings_catalog(settings),
            max_custom_tags=MAX_CUSTOM_TAGS,
            max_custom_tag_length=MAX_CUSTOM_TAG_LENGTH,
            private_job_tag_prefix=PRIVATE_JOB_TAG_PREFIX,
        )

        self.assertIn('id="settings-dialog"', html)
        self.assertIn("Settings &amp; Help", html)
        self.assertIn("TI_INFECTED_ACTION", html)
        self.assertIn("<code>delete</code>", html)
        self.assertIn("<code>hold</code>", html)
        self.assertIn("Use NAS Staging For Selected", html)
        self.assertIn('aria-describedby="nas-staging-action-help"', html)
        self.assertIn('id="nas-staging-action-help"', html)
        self.assertIn("This action changes only the temporary intake and download staging location", html)
        self.assertIn("This does not perform the final clean-library promotion", html)
        self.assertIn("/jobs/bulk-move-to-nas", html)
        self.assertNotIn("Move Selected To NAS", html)
        self.assertIn("qBittorrent Tags", html)
        self.assertIn('id="custom-tag-input"', html)
        self.assertIn('role="combobox"', html)
        self.assertIn('aria-controls="custom-tag-menu"', html)
        self.assertIn('id="custom-tag-menu"', html)
        self.assertIn('aria-label="Available qBittorrent tags"', html)
        self.assertIn('aria-multiselectable="true"', html)
        self.assertIn('id="custom-tag-status"', html)
        self.assertIn('aria-live="polite"', html)
        self.assertIn("/qbt/tags", html)
        self.assertIn("custom_tags", html)
        self.assertIn("private tracking tags are added separately and never shown here", html)
        self.assertIn("Tags selected in the main form apply to every torrent", html)
        self.assertIn(f"Maximum {MAX_CUSTOM_TAGS} tags, {MAX_CUSTOM_TAG_LENGTH} characters each", html)
        self.assertIn("Finish the tag search first", html)
        self.assertNotIn("commitPendingCustomTag", html)
        self.assertLess(
            html.index("matches.slice(0, existingLimit)"),
            html.index("if (canUseNewTag) options.push"),
        )
        self.assertIn("button.tabIndex = -1", html)
        self.assertNotIn("never-render-this-password", html)
        self.assertNotIn("never-render-this-token", html)


if __name__ == "__main__":
    unittest.main()
