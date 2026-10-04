from datetime import datetime
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base
from .tags import decode_custom_tags, encode_custom_tags


FINAL_DESTINATION_EDITABLE_STATES = (
    "downloading", "waiting_for_local_space", "waiting_for_nas", "waiting_for_qbt_hash",
)
FINAL_DESTINATION_LOCK_MARKERS = (
    "completion_event_received_at", "download_complete_at", "scan_completed_at",
    "promoted_at", "deleted_at", "threat_name", "quarantine_path", "hook_status",
)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    magnet_uri: Mapped[str] = mapped_column(Text, nullable=False)
    torrent_file_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    torrent_file_data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True, deferred=True)
    final_parent: Mapped[str] = mapped_column(Text, nullable=False)
    final_category: Mapped[str | None] = mapped_column(String(255), nullable=True)

    staging_preference: Mapped[str] = mapped_column(String(16), nullable=False)  # local | nas
    staging_actual: Mapped[str | None] = mapped_column(String(16), nullable=True)
    staging_root_initial: Mapped[str] = mapped_column(Text, nullable=False)
    staging_root_actual: Mapped[str | None] = mapped_column(Text, nullable=True)
    staging_overridden: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    override_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    nas_staging_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    nas_staging_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    nas_staging_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    nas_mount_marker: Mapped[str | None] = mapped_column(Text, nullable=True)

    managed_tag: Mapped[str] = mapped_column(String(255), nullable=False)
    unique_tag: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    custom_tags_json: Mapped[str] = mapped_column(
        Text,
        default="[]",
        server_default="[]",
        nullable=False,
    )
    qbt_hash: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    torrent_name: Mapped[str | None] = mapped_column(Text, nullable=True)

    state: Mapped[str] = mapped_column(String(64), default="submitted", nullable=False)
    is_terminal: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_seen_qbt_state: Mapped[str | None] = mapped_column(String(128), nullable=True)

    completion_event_received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    download_complete_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    scan_completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    quarantine_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    hook_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    hook_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    hook_destination: Mapped[str | None] = mapped_column(Text, nullable=True)
    hook_copy_source_root: Mapped[str | None] = mapped_column(Text, nullable=True)
    hook_copy_relative_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    hook_due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    hook_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    hook_finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    hook_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    hook_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    hook_script: Mapped[str | None] = mapped_column(Text, nullable=True)
    hook_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    hook_exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)

    threat_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    def final_destination_is_editable(self, *, has_scan_run: bool) -> bool:
        return (
            self.state in FINAL_DESTINATION_EDITABLE_STATES
            and not self.is_terminal
            and not has_scan_run
            and all(getattr(self, name) is None for name in FINAL_DESTINATION_LOCK_MARKERS)
        )

    @property
    def custom_tags(self) -> list[str]:
        return decode_custom_tags(self.custom_tags_json)

    @custom_tags.setter
    def custom_tags(self, values: list[str]) -> None:
        self.custom_tags_json = encode_custom_tags(values)


class ScanRun(Base):
    __tablename__ = "scan_runs"

    job_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pause_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    queued_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    root_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_files: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_files: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    current_file: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_file_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    verdict: Mapped[str | None] = mapped_column(String(16), nullable=True)
    scanner_version: Mapped[str | None] = mapped_column(String(255), nullable=True)
    engine_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    database_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    database_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    policy_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_scan_runs_lease", "lease_expires_at"),
        Index("ix_scan_runs_queue", "queued_at", "priority"),
    )


class ScanFile(Base):
    __tablename__ = "scan_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    mtime_ns: Mapped[int] = mapped_column(Integer, nullable=False)
    ctime_ns: Mapped[int | None] = mapped_column(Integer, nullable=True)
    device: Mapped[int | None] = mapped_column(Integer, nullable=True)
    inode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    threat_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    scanner_version: Mapped[str | None] = mapped_column(String(255), nullable=True)
    engine_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    database_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    database_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    policy_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    scan_method: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scan_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    scan_duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    scanned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("job_id", "relative_path", name="uq_scan_files_job_path"),
        Index("ix_scan_files_job_status", "job_id", "status"),
        Index("ix_scan_files_job_status_scanned", "job_id", "status", "scanned_at"),
    )


class ScannerControl(Base):
    __tablename__ = "scanner_control"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    requested_slots: Mapped[int] = mapped_column(Integer, nullable=False)
    boost_until_queue_empty: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    maintenance_mode: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    maintenance_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    maintenance_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
