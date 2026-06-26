from datetime import datetime
from typing import Literal
import re
from pydantic import BaseModel, Field, field_validator
from .config import get_settings


BTIH_PATTERN = re.compile(r"(^|[?&])xt=urn:btih:([A-Za-z0-9]{32}|[A-Fa-f0-9]{40})($|&)", re.IGNORECASE)
MAGNET_URI_PATTERN = re.compile(r"magnet:\?(?:(?!\s|magnet:\?).)+", re.IGNORECASE)


def extract_magnet_uris(value: str) -> list[str]:
    return [match.group(0).strip().rstrip("),];>") for match in MAGNET_URI_PATTERN.finditer(value or "")]


class JobCreate(BaseModel):
    magnet_uri: str = Field(min_length=10)
    final_parent: str = Field(min_length=2)
    final_category: str | None = None
    staging_preference: Literal["local", "nas"] = "local"

    @field_validator("final_parent")
    @classmethod
    def validate_final_parent(cls, value: str) -> str:
        settings = get_settings()
        for prefix in settings.allowed_final_parent_prefixes:
            normalized = prefix.rstrip("/")
            if value == normalized or value.startswith(f"{normalized}/"):
                return value
        allowed = ", ".join(settings.allowed_final_parent_prefixes)
        raise ValueError(f"final_parent must be inside one of: {allowed}")

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
    qbt_hash: str | None
    torrent_name: str | None
    state: str
    is_terminal: bool
    size_bytes: int | None
    content_path: str | None
    last_seen_qbt_state: str | None
    threat_name: str | None
    last_error: str | None
    progress: float | None = None
    eta_seconds: int | None = None
    download_speed_bytes_per_s: int | None = None
    upload_speed_bytes_per_s: int | None = None
    activity_summary: str | None = None

    model_config = {"from_attributes": True}


class JobSelectionIn(BaseModel):
    job_ids: list[str] = Field(default_factory=list)


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
