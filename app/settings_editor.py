"""Validated, redacted settings drafts; no running services are reconfigured here."""
from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import ValidationError

from .config import Settings, environment_settings, saved_settings
from .scanner import ScannerPolicyError, ScannerService
from .settings_view import ADVANCED_SETTINGS, NUMBER_LIMITS, SETTING_SPECS, display_setting, ui_editable
from .state_files import read_private


class SettingsEditError(ValueError):
    def __init__(self, fields: dict[str, str], status: int = 422):
        super().__init__("Check the highlighted settings; nothing was saved.")
        self.fields = fields
        self.status = status


def revision(settings: Settings) -> str:
    return hashlib.sha256(read_private(Path(settings.data_dir) / "settings.json")).hexdigest()


def pending_settings(settings: Settings) -> Settings | None:
    if not (Path(settings.data_dir) / "restart-required").exists():
        return None
    return Settings(**{**settings.model_dump(), **saved_settings()})


def validate_draft(settings: Settings, updates: object) -> tuple[Settings, list[dict]]:
    if not isinstance(updates, dict) or not updates:
        raise SettingsEditError({"_form": "Edit at least one setting first."})
    errors = {}
    overrides = environment_settings()
    for name in updates:
        if not ui_editable(name):
            errors[name] = "Deployment-only setting. Change it in Portainer or the local file while Intake is stopped."
        elif name in overrides:
            errors[name] = f"Remove TI_{name.upper()} from the container environment and redeploy before editing here."
    if errors:
        raise SettingsEditError(errors, 409)
    # Empty replacement inputs never erase credentials accidentally. Optional
    # values can be explicitly cleared with JSON null (a separate UI action).
    updates = {name: value for name, value in updates.items()
               if not (SETTING_SPECS[name].sensitive and value == "")}
    values = {**settings.model_dump(), **saved_settings(), **overrides, **updates}
    try:
        candidate = Settings(**values)
    except ValidationError as exc:
        raise SettingsEditError({str(error["loc"][0]): "Invalid value or type for this setting."
                                 for error in exc.errors(include_input=False)}) from exc
    for name in updates:
        value = getattr(candidate, name)
        if isinstance(value, int) and not isinstance(value, bool):
            minimum, maximum = NUMBER_LIMITS.get(name, (1, None))
            if value < minimum or (maximum is not None and value > maximum):
                errors[name] = f"Use a whole number from {minimum} to {maximum}." if maximum else f"Use a whole number of at least {minimum}."
        if name in {"qbt_host", "qbt_web_url"} and value is not None:
            try:
                parsed = urlsplit(value)
                valid = parsed.scheme in {"http", "https"} and parsed.hostname and parsed.port != 0
                valid = valid and not any(char.isspace() for char in value)
            except ValueError:
                valid = False
            if not valid:
                errors[name] = "Enter a complete http:// or https:// address, including the port if needed."
        if name in {"qbt_username", "ui_title", "intake_category"} and not value.strip():
            errors[name] = "This value cannot be empty."
    relationships = (
        ("scanner_definitions_warn_hours", "scanner_definitions_stale_hours", "Warning age cannot exceed the stale age."),
        ("max_concurrent_scans", "max_scan_slots", "Default concurrent scans cannot exceed the hard scan-slot ceiling."),
        ("max_concurrent_large_scans", "max_scan_slots", "Concurrent large scans cannot exceed the hard scan-slot ceiling."),
    )
    for lower, upper, message in relationships:
        if {lower, upper} & updates.keys() and getattr(candidate, lower) > getattr(candidate, upper):
            errors[lower] = errors[upper] = message
    if {"scan_heartbeat_seconds", "scan_lease_seconds"} & updates.keys() and candidate.scan_heartbeat_seconds >= candidate.scan_lease_seconds:
        errors["scan_heartbeat_seconds"] = "Heartbeat interval must be shorter than the scan lease."
    if errors:
        raise SettingsEditError(errors)
    scanner = ScannerService()
    scanner.settings = candidate
    try:
        scanner._validate_policy_configuration()
    except ScannerPolicyError as exc:
        message = str(exc)
        affected = {name: message for name in SETTING_SPECS if f"TI_{name.upper()}" in message}
        raise SettingsEditError(affected or {"_form": "Scanner configuration is invalid; verify scanner limits and installed media tools."}) from exc
    changes = []
    for name in updates:
        before, after = getattr(settings, name), getattr(candidate, name)
        if before != after:
            changes.append({
                "name": name, "label": SETTING_SPECS[name].label,
                "before": display_setting(name, before),
                "after": "New value (hidden)" if SETTING_SPECS[name].sensitive and after is not None else display_setting(name, after),
                "advanced": name in ADVANCED_SETTINGS,
            })
    return candidate, changes
