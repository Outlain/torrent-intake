from functools import lru_cache
from pathlib import Path
from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    clamdscan_binary: str = "clamscan"
    clamdscan_args: str = "--infected --no-summary --recursive"

    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    ui_title: str = "Torrent Intake"

    model_config = SettingsConfigDict(
        env_prefix="TI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def local_max_bytes(self) -> int:
        return self.local_max_gib * 1024 * 1024 * 1024

    @property
    def local_free_space_buffer_bytes(self) -> int:
        return self.local_free_space_buffer_gib * 1024 * 1024 * 1024

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
