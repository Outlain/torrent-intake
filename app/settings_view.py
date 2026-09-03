from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .config import Settings


@dataclass(frozen=True)
class SettingSpec:
    category: str
    label: str
    description: str
    sensitive: bool = False
    safety_critical: bool = False
    url_value: bool = False
    change_hint: str | None = None
    value_explanations: dict[str, str] | None = None


CATEGORY_DETAILS = {
    "Application": "Application identity, persistence, logging behavior, and UI presentation.",
    "qBittorrent": "Connection, authentication, tagging, and category behavior for the managed qBittorrent instance.",
    "Storage and placement": "Container-visible staging roots, approved destinations, and local-to-NAS capacity policy.",
    "ClamAV scanner": "Private ClamD communication, definition freshness, file limits, and scan deadlines.",
    "Scan scheduling": "Bounded concurrency, leases, retry timing, and cooperative scan behavior.",
    "Infection and events": "The safety-critical infection action, quarantine boundary, and durable event spool.",
}


DEFAULT_CHANGE_HINT = (
    "Deployment managed. Change the corresponding environment variable in Compose or Portainer, "
    "then recreate Torrent Intake."
)


SETTING_SPECS: dict[str, SettingSpec] = {
    "app_name": SettingSpec(
        "Application", "Application name", "Internal application identifier used by the service."
    ),
    "debug": SettingSpec(
        "Application", "Debug logging", "Enables verbose application logging. Leave disabled for normal operation."
    ),
    "database_url": SettingSpec(
        "Application",
        "Database location",
        "Persistent job, checkpoint, migration, and scanner-control database. Credentials are hidden for non-SQLite URLs.",
        url_value=True,
    ),
    "ui_title": SettingSpec(
        "Application", "UI title", "Heading displayed at the top of the Torrent Intake interface."
    ),
    "qbt_host": SettingSpec(
        "qBittorrent",
        "qBittorrent API URL",
        "Private Web API endpoint reachable from Torrent Intake. It must match the existing Gluetun networking arrangement.",
        url_value=True,
    ),
    "qbt_username": SettingSpec(
        "qBittorrent", "qBittorrent username", "Account name used for authenticated qBittorrent API calls."
    ),
    "qbt_password": SettingSpec(
        "qBittorrent",
        "qBittorrent password",
        "Password used for qBittorrent API authentication. The value is never returned to the UI.",
        sensitive=True,
    ),
    "qbt_verify_certificate": SettingSpec(
        "qBittorrent",
        "Verify qBittorrent TLS certificate",
        "Validates the qBittorrent HTTPS certificate when TLS is used.",
        safety_critical=True,
        value_explanations={
            "Enabled": "TLS certificate validation is enabled.",
            "Disabled": "TLS certificate validation is disabled; use only on a trusted private path.",
        },
    ),
    "qbt_request_timeout_seconds": SettingSpec(
        "qBittorrent", "qBittorrent request timeout", "Maximum wait for an individual qBittorrent API request, in seconds."
    ),
    "qbt_web_url": SettingSpec(
        "qBittorrent",
        "Browser qBittorrent URL",
        "Browser-safe link shown in the UI. This can differ from the private API URL used by the container.",
        url_value=True,
    ),
    "intake_category": SettingSpec(
        "qBittorrent", "Intake category", "qBittorrent category assigned while a torrent is managed by Intake."
    ),
    "managed_tag": SettingSpec(
        "qBittorrent",
        "Managed tag",
        "Required qBittorrent tag proving that a torrent remains under Torrent Intake control.",
        safety_critical=True,
    ),
    "auto_create_final_category": SettingSpec(
        "qBittorrent",
        "Create final categories",
        "Allows Torrent Intake to create a requested final qBittorrent category when it does not already exist.",
    ),
    "completion_event_token": SettingSpec(
        "qBittorrent",
        "Completion-hook token",
        "Authenticates qBittorrent completion callbacks. The value is never returned to the UI.",
        sensitive=True,
    ),
    "local_staging_root": SettingSpec(
        "Storage and placement",
        "Local staging root",
        "Exact container path used for locally staged torrents. qBittorrent must see the same content at the same path.",
        safety_critical=True,
    ),
    "nas_staging_root": SettingSpec(
        "Storage and placement",
        "NAS staging root",
        "Exact container path used for temporary NAS intake/download staging. This is separate from the final clean-library destination, and qBittorrent must use the same path.",
        safety_critical=True,
    ),
    "final_parent_prefix": SettingSpec(
        "Storage and placement",
        "Primary final root",
        "Primary canonical media boundary allowed for clean promotion destinations.",
        safety_critical=True,
    ),
    "final_parent_prefixes": SettingSpec(
        "Storage and placement",
        "Additional final roots",
        "Optional comma-separated canonical media boundaries allowed in addition to the primary final root.",
        safety_critical=True,
    ),
    "local_overflow_policy": SettingSpec(
        "Storage and placement",
        "Local overflow policy",
        "Action taken when aggregate local staging capacity is unavailable for another torrent.",
        value_explanations={
            "queue": "Wait for local capacity while preserving the requested local staging preference.",
            "nas": "Switch eligible work to temporary NAS intake staging when local capacity is unavailable; the chosen final destination does not change.",
        },
    ),
    "local_max_gib": SettingSpec(
        "Storage and placement",
        "Maximum local torrent size",
        "A torrent larger than this GiB limit is directed to NAS staging instead of local staging.",
    ),
    "local_free_space_buffer_gib": SettingSpec(
        "Storage and placement",
        "Local free-space reserve",
        "GiB kept unused when Torrent Intake calculates safe local staging capacity.",
    ),
    "polling_interval_seconds": SettingSpec(
        "Storage and placement",
        "Recovery polling interval",
        "How often the background reconciliation pass checks qBittorrent when no completion callback arrives.",
    ),
    "completion_grace_seconds": SettingSpec(
        "Storage and placement",
        "Completion grace period",
        "Delay after a completion signal before Torrent Intake begins its final completion and pause checks.",
    ),
    "scanner_backend": SettingSpec(
        "ClamAV scanner",
        "Scanner backend",
        "Scanning implementation. The supported backend is the persistent private ClamD sidecar.",
        safety_critical=True,
    ),
    "clamd_socket_path": SettingSpec(
        "ClamAV scanner",
        "ClamD socket",
        "Private Unix socket shared only with the Torrent Intake ClamD sidecar.",
        safety_critical=True,
    ),
    "scanner_policy_version": SettingSpec(
        "ClamAV scanner",
        "Scanner policy version",
        "Identity of the scan policy stored with checkpoints. Changing it invalidates incompatible clean checkpoints.",
        safety_critical=True,
    ),
    "scanner_max_file_mib": SettingSpec(
        "ClamAV scanner",
        "Native ClamD stream boundary",
        "Raw-size boundary for one native ClamD stream. Larger verified videos and raw TrueHD audio, plus eligible media whose native parser reaches MaxScanSize, use bounded overlapping windows; other unsupported content remains held.",
        safety_critical=True,
    ),
    "scanner_health_cache_seconds": SettingSpec(
        "ClamAV scanner", "Scanner health cache", "How long a successful ClamD health result may be reused, in seconds."
    ),
    "scanner_connect_timeout_seconds": SettingSpec(
        "ClamAV scanner", "ClamD connection timeout", "Maximum time to establish a private socket connection, in seconds."
    ),
    "scanner_scan_timeout_seconds": SettingSpec(
        "ClamAV scanner", "Per-file scan timeout", "Maximum total client wait for one streamed file scan, in seconds."
    ),
    "scanner_definitions_warn_hours": SettingSpec(
        "ClamAV scanner",
        "Definition warning age",
        "Definition age in hours at which the UI and events begin warning before scans are blocked.",
    ),
    "scanner_definitions_stale_hours": SettingSpec(
        "ClamAV scanner",
        "Definition stale age",
        "Definition age in hours at which scanning fails closed until current definitions are available.",
        safety_critical=True,
    ),
    "large_media_enabled": SettingSpec(
        "ClamAV scanner",
        "Large-media scanner",
        "Routes oversized verified videos and narrowly validated raw TrueHD audio, plus eligible media whose native scan reaches a parser/expanded-data limit, through full-byte overlapping ClamD windows.",
        safety_critical=True,
    ),
    "large_media_max_file_gib": SettingSpec(
        "ClamAV scanner",
        "Maximum large-media size",
        "Hard GiB ceiling for the bounded large-media path. Larger individual files remain held without a clean verdict.",
        safety_critical=True,
    ),
    "large_media_chunk_mib": SettingSpec(
        "ClamAV scanner",
        "Large-media ClamD window",
        "MiB sent in each independent ClamD window. It must remain below the native ClamD stream limit.",
        safety_critical=True,
    ),
    "large_media_min_chunk_mib": SettingSpec(
        "ClamAV scanner",
        "Minimum adaptive media window",
        "Smallest MiB window allowed when ClamD reaches a parser or expanded-data limit and Torrent Intake safely subdivides that range.",
        safety_critical=True,
    ),
    "large_media_overlap_kib": SettingSpec(
        "ClamAV scanner",
        "Large-media window overlap",
        "KiB repeated between neighboring windows so signatures crossing a window boundary are still visible.",
        safety_critical=True,
    ),
    "large_media_probe_timeout_seconds": SettingSpec(
        "ClamAV scanner",
        "Media validation timeout",
        "Maximum time allowed for ffprobe to verify the real container and stream layout of an oversized file.",
    ),
    "large_media_scan_timeout_seconds": SettingSpec(
        "ClamAV scanner",
        "Large-media scan timeout",
        "Total deadline for validating and reading every window of one oversized media file.",
    ),
    "ffprobe_binary": SettingSpec(
        "ClamAV scanner",
        "ffprobe executable",
        "Image-provided ffprobe path used to validate oversized media. The Docker image installs it at build time.",
        safety_critical=True,
    ),
    "per_job_scan_workers": SettingSpec(
        "Scan scheduling",
        "Large-file workers per torrent",
        "Maximum large-media byte ranges from one torrent that may be streamed to ClamD simultaneously.",
    ),
    "clamd_max_inflight_requests": SettingSpec(
        "Scan scheduling",
        "Global ClamD request limit",
        "Hard application-wide limit for simultaneous ClamD INSTREAM requests across every active torrent. Keep it at or below ClamD MaxThreads.",
        safety_critical=True,
    ),
    "max_concurrent_scans": SettingSpec(
        "Scan scheduling",
        "Default concurrent scans",
        "Normal number of scan workers. The Scan Queue control can temporarily change the active slot request.",
        change_hint="Use the Scan Queue slot control for a live adjustment. Change this environment value to alter the restart default.",
    ),
    "max_scan_slots": SettingSpec(
        "Scan scheduling",
        "Hard scan-slot ceiling",
        "Maximum slot count the runtime Scan Queue control is permitted to request.",
        safety_critical=True,
    ),
    "max_concurrent_large_scans": SettingSpec(
        "Scan scheduling",
        "Concurrent large scans",
        "Maximum number of torrents above the large-job threshold that can scan at the same time.",
    ),
    "large_scan_gib": SettingSpec(
        "Scan scheduling", "Large-job threshold", "Torrent size in GiB at which the separate large-scan concurrency bound applies."
    ),
    "scan_scheduler_interval_seconds": SettingSpec(
        "Scan scheduling", "Scan scheduler interval", "Delay between scheduler attempts to claim pending scan work, in seconds."
    ),
    "scan_lease_seconds": SettingSpec(
        "Scan scheduling", "Scan lease duration", "Restart-recovery lease protecting an actively owned scan, in seconds."
    ),
    "scan_heartbeat_seconds": SettingSpec(
        "Scan scheduling", "Scan heartbeat interval", "How frequently an active worker refreshes its durable scan lease, in seconds."
    ),
    "scan_retry_base_seconds": SettingSpec(
        "Scan scheduling", "Scan retry base delay", "Initial delay used by exponential retry after scanner failures, in seconds."
    ),
    "scan_max_failures": SettingSpec(
        "Scan scheduling", "Maximum scan failures", "Failure count after which a file/job requires explicit operator retry."
    ),
    "scan_yield_after_files": SettingSpec(
        "Scan scheduling",
        "Files before scheduler yield",
        "Number of successfully checkpointed files processed before a worker yields so other queued jobs can progress.",
    ),
    "pause_confirmation_timeout_seconds": SettingSpec(
        "Scan scheduling",
        "Pause confirmation timeout",
        "Maximum wait for qBittorrent to confirm a torrent is paused before scanning or a destructive action.",
        safety_critical=True,
    ),
    "infected_action": SettingSpec(
        "Infection and events",
        "Infected-content action",
        "Action after a verified malware detection. This safety-critical value cannot be changed from the UI.",
        safety_critical=True,
        value_explanations={
            "hold": "Leave the torrent paused in its verified staging location. This is the built-in safe default.",
            "quarantine": "Ask qBittorrent to move the torrent into an exclusive quarantine directory; both containers need the same /quarantine mount.",
            "delete": "After repeating all safety checks, ask qBittorrent to remove the torrent and delete its data, then verify removal.",
        },
    ),
    "quarantine_root": SettingSpec(
        "Infection and events",
        "Torrent quarantine root",
        "Canonical quarantine boundary used only when the infected-content action is quarantine.",
        safety_critical=True,
    ),
    "event_dir": SettingSpec(
        "Infection and events",
        "Structured event directory",
        "Writable spool where Torrent Intake atomically emits notifier events. It must be persistent.",
        safety_critical=True,
    ),
}


def _secret_is_configured(value: Any) -> bool:
    if value is None:
        return False
    text = str(value).strip()
    return bool(text and not text.upper().startswith("REPLACE_"))


def _safe_url(value: Any) -> str:
    if value is None or not str(value).strip():
        return "Not configured"
    text = str(value).strip()
    if text.startswith("sqlite:"):
        return text
    try:
        parsed = urlsplit(text)
    except ValueError:
        return "Configured URL (details hidden)"
    if not parsed.scheme or not parsed.hostname:
        return "Configured URL (details hidden)"
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        port = ""
    credentials = "hidden@" if parsed.username is not None or parsed.password is not None else ""
    return urlunsplit((parsed.scheme, f"{credentials}{host}{port}", parsed.path, "", ""))


def _display_value(spec: SettingSpec, value: Any, *, default: bool = False) -> str:
    if spec.sensitive:
        if default:
            return "Required deployment secret" if value is not None else "Not configured"
        return "Configured (hidden)" if _secret_is_configured(value) else "Not configured"
    if spec.url_value:
        return _safe_url(value)
    if value is None or value == "":
        return "Not configured"
    if isinstance(value, bool):
        return "Enabled" if value else "Disabled"
    return str(value)


def build_settings_catalog(settings: Settings) -> list[dict[str, object]]:
    grouped: dict[str, list[dict[str, object]]] = {
        category: [] for category in CATEGORY_DETAILS
    }
    for name, spec in SETTING_SPECS.items():
        field = Settings.model_fields[name]
        current_value = getattr(settings, name)
        current_display = _display_value(spec, current_value)
        default_display = _display_value(spec, field.default, default=True)
        current_effect = None
        if spec.value_explanations:
            current_effect = spec.value_explanations.get(current_display)
        grouped[spec.category].append(
            {
                "name": name,
                "env_name": f"TI_{name.upper()}",
                "label": spec.label,
                "description": spec.description,
                "current": current_display,
                "default": default_display,
                "current_effect": current_effect,
                "sensitive": spec.sensitive,
                "safety_critical": spec.safety_critical,
                "change_hint": spec.change_hint or DEFAULT_CHANGE_HINT,
            }
        )

    return [
        {
            "name": category,
            "description": description,
            "settings": grouped[category],
        }
        for category, description in CATEGORY_DETAILS.items()
    ]
