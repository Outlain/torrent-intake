from datetime import datetime
from typing import Literal
import re
from pydantic import BaseModel, Field, field_validator
from .config import get_settings
from .paths import canonical_final_parent
from .tags import MAX_CUSTOM_TAGS, normalize_custom_tags


BTIH_PATTERN = re.compile(r"(^|[?&])xt=urn:btih:([A-Za-z0-9]{32}|[A-Fa-f0-9]{40})($|&)", re.IGNORECASE)
MAGNET_URI_PATTERN = re.compile(r"magnet:\?(?:(?!\s|magnet:\?).)+", re.IGNORECASE)


def extract_magnet_uris(value: str) -> list[str]:
    return [match.group(0).strip().rstrip("),];>") for match in MAGNET_URI_PATTERN.finditer(value or "")]


class JobCreate(BaseModel):
    magnet_uri: str = Field(min_length=10)
    final_parent: str = Field(min_length=2)
    final_category: str | None = None
    staging_preference: Literal["local", "nas"] = "local"
    custom_tags: list[str] = Field(default_factory=list, max_length=MAX_CUSTOM_TAGS)

    @field_validator("final_parent")
    @classmethod
    def validate_final_parent(cls, value: str) -> str:
        return canonical_final_parent(value, get_settings())

    @field_validator("magnet_uri")
    @classmethod
    def validate_magnet(cls, value: str) -> str:
        magnets = extract_magnet_uris(value)
        if len(magnets) > 1:
            raise ValueError("submit multiple magnet links through /jobs/bulk so each torrent gets its own intake job")
        value = magnets[0] if magnets else value.strip()
        if not value.lower().startswith("magnet:?"):
            raise ValueError("Only magnet links are supported in this MVP")
        # Require a plausible BTIH hash to avoid opaque downstream qBittorrent errors.
        if not BTIH_PATTERN.search(value):
            raise ValueError("magnet_uri must include a valid xt=urn:btih hash")
        return value

    @field_validator("custom_tags")
    @classmethod
    def validate_custom_tags(cls, value: list[str]) -> list[str]:
        return normalize_custom_tags(
            value,
            reserved_tags=(get_settings().managed_tag,),
        )


class JobBatchCreate(BaseModel):
    jobs: list[JobCreate] = Field(min_length=1, max_length=50)


class JobOut(BaseModel):
    id: str
    created_at: datetime
    updated_at: datetime
    magnet_uri: str
    final_parent: str
    final_category: str | None
    staging_preference: str
    staging_actual: str | None
    staging_root_initial: str
    staging_root_actual: str | None
    staging_overridden: bool
    override_reason: str | None
    managed_tag: str
    unique_tag: str
    custom_tags: list[str]
    qbt_hash: str | None
    torrent_name: str | None
    state: str
    is_terminal: bool
    size_bytes: int | None
    content_path: str | None
    last_seen_qbt_state: str | None
    threat_name: str | None
    quarantine_path: str | None
    last_error: str | None
    progress: float | None = None
    eta_seconds: int | None = None
    download_speed_bytes_per_s: int | None = None
    upload_speed_bytes_per_s: int | None = None
    activity_summary: str | None = None
    scan_priority: int = 0
    scan_pause_requested: bool = False
    scan_total_files: int = 0
    scan_completed_files: int = 0
    scan_total_bytes: int = 0
    scan_completed_bytes: int = 0
    scan_current_file: str | None = None
    scan_current_file_started_at: datetime | None = None
    scan_queue_position: int | None = None
    scan_attempts: int = 0
    scan_last_error: str | None = None
    scan_is_large: bool = False
    scan_progress_percent: float | None = None
    scan_eta_seconds: int | None = None
    scan_eta_confidence: str | None = None
    scan_engine_version: str | None = None
    scan_database_version: str | None = None
    scan_database_updated_at: datetime | None = None
    scan_policy_version: str | None = None

    model_config = {"from_attributes": True}


class JobSelectionIn(BaseModel):
    job_ids: list[str] = Field(default_factory=list)


class ScannerSlotsUpdate(BaseModel):
    slots: int = Field(ge=1)


class ScannerMaintenanceUpdate(BaseModel):
    enabled: bool
    reason: str | None = Field(default=None, max_length=500)


class JobBulkResult(BaseModel):
    requested: int
    processed: int
    skipped: int
    failed: int
    processed_ids: list[str] = Field(default_factory=list)
    skipped_ids: list[str] = Field(default_factory=list)
    failed_ids: list[str] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)


class JobBatchCreateResult(BaseModel):
    requested: int
    created: int
    failed: int
    jobs: list[JobOut] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)


class CompletionEventIn(BaseModel):
    qbt_hash: str | None = None
    qbt_hash_v2: str | None = None
    torrent_name: str | None = None
    content_path: str | None = None
    root_path: str | None = None
    save_path: str | None = None
    category: str | None = None
    tags: str | None = None
    tracker: str | None = None
    size_bytes: int | None = None
    files_count: int | None = None
    torrent_id: str | None = None
    token: str | None = None
    unique_tag: str | None = None
