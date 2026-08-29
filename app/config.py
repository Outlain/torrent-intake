from functools import lru_cache
from pathlib import Path
from typing import Literal
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .tags import normalize_managed_tag


class Settings(BaseSettings):
    app_name: str = "torrent-intake"
    debug: bool = False
    database_url: str = "sqlite:////app/data/torrent_intake.db"

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
    scanner_policy_version: str = "clamav-policy-v4-parallel-adaptive-media"
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

    ui_title: str = "Torrent Intake"

    model_config = SettingsConfigDict(
        env_prefix="TI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("managed_tag")
    @classmethod
    def validate_managed_tag(cls, value: str) -> str:
        # qBittorrent silently ignores invalid tags on torrent-add requests.
        # This tag is an ownership credential, so fail startup instead.
        return normalize_managed_tag(value)

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
