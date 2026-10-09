import json
from functools import lru_cache
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, DotEnvSettingsSource, EnvSettingsSource, SettingsConfigDict

from .tags import normalize_managed_tag
from .state_files import data_directory, read_private, write_json
from .paths import canonical_final_parent


def saved_settings() -> dict:
    try:
        payload = json.loads(read_private(data_directory() / "settings.json"))
    except FileNotFoundError:
        return {}
    if not isinstance(payload, dict) or payload.get("schema_version") != 1 or not isinstance(payload.get("settings"), dict):
        raise ValueError("settings.json must contain schema_version=1 and a settings object")
    unknown = set(payload["settings"]) - set(Settings.model_fields)
    if unknown:
        raise ValueError("settings.json contains unknown setting names: " + ", ".join(sorted(unknown)))
    values = dict(payload["settings"])
    # This is the bootstrap mount location, never selected by an imported file.
    values.pop("data_dir", None)
    return values


class NasStagingLocation(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    label: str = Field(min_length=1, max_length=100)
    path: str
    mount_marker: str | None = None

    @field_validator("path", "mount_marker")
    @classmethod
    def absolute_path(cls, value):
        if value is None:
            return None
        if not value.startswith("/") or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Use an absolute container path without control characters")
        if ".." in Path(value).parts:
            raise ValueError("Parent traversal is not allowed")
        return str(Path(value))

    @field_validator("label")
    @classmethod
    def clean_label(cls, value):
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("Use a non-empty label without control characters")
        return value.strip()


def copy_destination(value: str | None) -> str | None:
    if value is None:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Copy destination must not contain control characters")
    value = value.strip()
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or not path.is_relative_to("/copy-target"):
        raise ValueError("Copy destination must be /copy-target or a subdirectory of that mount")
    return str(path)


class PostPromotionCopyRule(BaseModel):
    source: str = Field(min_length=1, max_length=4096)
    destination: str = Field(min_length=1, max_length=4096)
    enabled: bool = True

    model_config = {"extra": "forbid"}

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        if (not Path(value).is_absolute() or ".." in Path(value).parts
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise ValueError("Copy source must be an absolute container path without traversal or control characters")
        return str(Path(value))

    @field_validator("destination")
    @classmethod
    def validate_destination(cls, value: str) -> str:
        result = copy_destination(value)
        if result is None:
            raise ValueError("Configure a destination for each copy rule")
        return result


class Settings(BaseSettings):
    app_name: str = "torrent-intake"
    debug: bool = False
    data_dir: str = Field(default_factory=lambda: str(data_directory()))
    database_url: str = Field(default_factory=lambda: f"sqlite:///{data_directory() / 'torrent_intake.db'}")

    qbt_host: str = "http://qbittorrent:8080"
    qbt_username: str = "admin"
    qbt_password: str = "REPLACE_WITH_STRONG_PASSWORD"
    qbt_verify_certificate: bool = False
    qbt_request_timeout_seconds: int = 20
    qbt_web_url: str | None = None

    intake_category: str = "intake"
    managed_tag: str = "torrent_intake"
    auto_create_final_category: bool = True

    local_staging_root: str = "/staging-local"
    nas_staging_root: str = "/downloads/torrent-intake/staging"
    nas_staging_locations: list[NasStagingLocation] = Field(default_factory=list, max_length=32)
    default_nas_staging_id: str | None = None
    final_parent_prefix: str = "/downloads"
    final_parent_prefixes: str | None = None

    local_overflow_policy: Literal["queue", "nas"] = "queue"
    local_max_gib: int = 200
    local_free_space_buffer_gib: int = 5
    polling_interval_seconds: int = 300
    completion_grace_seconds: int = 15
    completion_event_token: str | None = None

    scanner_backend: Literal["clamd"] = "clamd"
    clamd_socket_path: str = "/run/clamav/clamd.sock"
    scanner_policy_version: str = "clamav-policy-v5-media-attachments"
    scanner_max_file_mib: int = 2000
    scanner_health_cache_seconds: int = 15
    scanner_connect_timeout_seconds: int = 5
    scanner_scan_timeout_seconds: int = 1200
    scanner_definitions_warn_hours: int = 36
    scanner_definitions_stale_hours: int = 72
    large_media_enabled: bool = True
    large_media_max_file_gib: int = 100
    large_media_chunk_mib: int = 512
    large_media_min_chunk_mib: int = 64
    large_media_overlap_kib: int = 1024
    large_media_probe_timeout_seconds: int = 120
    large_media_scan_timeout_seconds: int = 172800
    ffprobe_binary: str = "/usr/bin/ffprobe"
    ffmpeg_binary: str = "/usr/bin/ffmpeg"
    media_attachment_max_mib: int = 16
    media_attachment_total_mib: int = 64
    archive_enabled: bool = True
    archive_scratch_dir: str | None = None
    archive_scratch_mount_marker: str | None = None
    archive_max_file_gib: int = Field(default=100, ge=1, le=1024)
    archive_max_expanded_gib: int = Field(default=20, ge=1, le=1024)
    archive_max_files: int = Field(default=10000, ge=1, le=100000)
    archive_max_depth: int = Field(default=4, ge=1, le=8)
    archive_scan_timeout_seconds: int = Field(default=7200, ge=1, le=172800)
    archive_free_space_buffer_gib: int = Field(default=1, ge=1, le=1024)
    per_job_scan_workers: int = 1
    clamd_max_inflight_requests: int = 4
    max_concurrent_scans: int = 2
    max_scan_slots: int = 4
    max_concurrent_large_scans: int = 1
    large_scan_gib: int = 2
    scan_scheduler_interval_seconds: int = 3
    scan_lease_seconds: int = 90
    scan_heartbeat_seconds: int = 10
    scan_retry_base_seconds: int = 30
    scan_max_failures: int = 3
    scan_yield_after_files: int = 10
    pause_confirmation_timeout_seconds: int = 30

    infected_action: Literal["hold", "quarantine", "delete"] = "hold"
    quarantine_root: str = "/quarantine"
    event_dir: str = "/events"

    post_promotion_enabled: bool = False
    post_promotion_script: str | None = None
    post_promotion_copy_enabled: bool = False
    post_promotion_copy_destination: str | None = None
    post_promotion_copy_rules: list[PostPromotionCopyRule] = Field(default_factory=list, max_length=32)
    post_promotion_delay_seconds: int = Field(default=5, ge=0, le=3600)
    post_promotion_timeout_seconds: int = Field(default=7200, ge=1, le=604800)

    ui_title: str = "Torrent Intake"

    model_config = SettingsConfigDict(
        env_prefix="TI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings):
        return init_settings, env_settings, dotenv_settings, saved_settings, file_secret_settings

    @field_validator("managed_tag")
    @classmethod
    def validate_managed_tag(cls, value: str) -> str:
        # qBittorrent silently ignores invalid tags on torrent-add requests.
        # This tag is an ownership credential, so fail startup instead.
        return normalize_managed_tag(value)

    @field_validator("post_promotion_copy_destination")
    @classmethod
    def validate_copy_destination(cls, value: str | None) -> str | None:
        return copy_destination(value)

    @field_validator("archive_scratch_dir", "archive_scratch_mount_marker")
    @classmethod
    def validate_archive_storage_path(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        path = Path(value)
        if (not path.is_absolute() or ".." in path.parts or path == Path("/")
                or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ValueError("Use an absolute archive storage path without traversal or control characters")
        return str(path)

    @model_validator(mode="after")
    def validate_archive_storage(self):
        if self.archive_scratch_mount_marker:
            if not self.archive_scratch_dir:
                raise ValueError("Configure an archive scratch directory before its mount marker")
            marker = Path(self.archive_scratch_mount_marker)
            if not marker.is_relative_to(self.archive_scratch_dir) or str(marker) == self.archive_scratch_dir:
                raise ValueError("Archive scratch mount marker must be a file inside the scratch directory")
        if self.archive_scratch_dir:
            scratch = Path(self.archive_workspace_root).resolve()
            content_roots = (
                self.local_staging_root, self.nas_staging_root,
                *(location.path for location in self.effective_nas_locations),
                self.quarantine_root, self.event_dir, "/copy-target",
            )
            for value in content_roots:
                protected = Path(value).resolve()
                if scratch.is_relative_to(protected) or protected.is_relative_to(scratch):
                    raise ValueError("Archive scratch space must not overlap torrent staging, quarantine, events or copy targets")
        return self

    @property
    def archive_workspace_root(self) -> str:
        return str(Path(self.archive_scratch_dir or self.data_dir) / "archive-scan")

    @model_validator(mode="after")
    def validate_locations_and_hook(self):
        locations = self.effective_nas_locations
        ids = [location.id for location in locations]
        if len(set(ids)) != len(ids):
            raise ValueError("NAS location IDs must be unique")
        if self.default_nas_staging_id is not None and self.default_nas_staging_id not in ids:
            raise ValueError("The default NAS location must refer to a configured location")
        if len(locations) > 1 and not self.default_nas_staging_id:
            raise ValueError("Select exactly one automatic NAS default")
        # Check explicit new registries strictly without breaking old mount layouts.
        roots = [Path(self.local_staging_root).resolve()]
        operational = [Path(value).resolve() for value in (
            "/app", "/state", "/events", "/hooks", "/copy-target", "/var/lib/clamav", "/quarantine",
            "/downloads/docker", self.data_dir, self.event_dir, self.quarantine_root,
        )]
        for location in self.nas_staging_locations:
            root = Path(location.path).resolve()
            if any(root == p or p in root.parents or root in p.parents for p in operational):
                raise ValueError("NAS staging cannot use an operational directory")
            if any(root == p or root in p.parents or p in root.parents for p in roots):
                raise ValueError("Staging directories must not duplicate or overlap one another")
            if str(root) in self.allowed_final_parent_prefixes:
                raise ValueError("Use a staging subdirectory, not an entire final media root")
            roots.append(root)
        if self.post_promotion_script:
            path = Path(self.post_promotion_script)
            if not path.is_absolute() or ".." in path.parts or Path("/hooks") not in path.parents:
                raise ValueError("The trusted post-promotion executable must be under /hooks")
        if self.post_promotion_enabled and not self.post_promotion_script:
            raise ValueError("Configure TI_POST_PROMOTION_SCRIPT before enabling the hook")
        if self.post_promotion_copy_enabled and self.post_promotion_enabled:
            raise ValueError("Choose either built-in copying or a custom script, not both")
        seen_sources = set()
        for rule in self.post_promotion_copy_rules:
            rule.source = canonical_final_parent(rule.source, self)
            if rule.source in seen_sources:
                raise ValueError("Copy rules must have unique source folders, including disabled rules")
            seen_sources.add(rule.source)
            source = Path(rule.source)
            if any(source.is_relative_to(path) for path in operational):
                raise ValueError("Copy source cannot use an operational directory")
            try:
                destination, copy_root = Path(rule.destination).resolve(), Path("/copy-target").resolve()
            except (OSError, RuntimeError) as exc:
                raise ValueError("Copy destination could not be resolved safely") from exc
            if not destination.is_relative_to(copy_root):
                raise ValueError("Copy destination must stay inside the dedicated /copy-target mount")
            if source.is_relative_to(destination) or destination.is_relative_to(source):
                raise ValueError("Copy source and destination must not be identical or nested")
            try:
                aliases_source = source.samefile(destination)
            except OSError:
                aliases_source = False  # An offline mount is handled by the runner, not settings validation.
            if aliases_source:
                raise ValueError("Copy destination aliases its source; choose separate storage")
        return self

    @property
    def effective_nas_locations(self) -> list[NasStagingLocation]:
        return self.nas_staging_locations or [NasStagingLocation(
            id="primary", label="Main NAS", path=self.nas_staging_root,
        )]

    @property
    def default_nas_location(self) -> NasStagingLocation:
        return self.nas_location(self.default_nas_staging_id)

    def nas_location(self, location_id: str | None = None) -> NasStagingLocation:
        location_id = location_id or self.default_nas_staging_id or self.effective_nas_locations[0].id
        for location in self.effective_nas_locations:
            if location.id == location_id:
                return location
        raise ValueError(f"Unknown NAS staging location: {location_id}")

    @property
    def local_max_bytes(self) -> int:
        return self.local_max_gib * 1024 * 1024 * 1024

    @property
    def local_free_space_buffer_bytes(self) -> int:
        return self.local_free_space_buffer_gib * 1024 * 1024 * 1024

    @property
    def large_scan_bytes(self) -> int:
        return self.large_scan_gib * 1024 * 1024 * 1024

    @property
    def scanner_max_file_bytes(self) -> int:
        return self.scanner_max_file_mib * 1024 * 1024

    @property
    def large_media_max_file_bytes(self) -> int:
        return self.large_media_max_file_gib * 1024 * 1024 * 1024

    @property
    def large_media_chunk_bytes(self) -> int:
        return self.large_media_chunk_mib * 1024 * 1024

    @property
    def large_media_overlap_bytes(self) -> int:
        return self.large_media_overlap_kib * 1024

    @property
    def large_media_min_chunk_bytes(self) -> int:
        return self.large_media_min_chunk_mib * 1024 * 1024

    @property
    def allowed_final_parent_prefixes(self) -> list[str]:
        values = [self.final_parent_prefix]
        if self.final_parent_prefixes:
            values.extend(part.strip() for part in self.final_parent_prefixes.split(","))

        unique_values: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not value:
                continue
            normalized = str(Path(value).resolve())
            if normalized in seen:
                continue
            seen.add(normalized)
            unique_values.append(normalized)
        return unique_values

    @property
    def extra_final_parent_prefixes(self) -> list[str]:
        allowed = self.allowed_final_parent_prefixes
        if not allowed:
            return []
        primary = allowed[0]
        return [value for value in allowed if value != primary]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def environment_settings() -> dict:
    """Explicit environment/dotenv values, using the same parsing as Settings."""
    return {**DotEnvSettingsSource(Settings)(), **EnvSettingsSource(Settings)()}


def persist_settings(settings: Settings) -> None:
    if settings.data_dir != str(data_directory()):
        raise ValueError("TI_DATA_DIR must be set in the container environment, not in a dotenv/settings file")
    path = data_directory() / "settings.json"
    payload = {"schema_version": 1, "settings": settings.model_dump(mode="json")}
    try:
        if json.loads(read_private(path)) == payload:
            path.chmod(0o600)
            return
    except FileNotFoundError:
        pass
    write_json(path, payload)
