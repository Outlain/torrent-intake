from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .config import get_settings


VERSION_PATTERN = re.compile(r"ClamAV\s+([^/\s]+)/([^/\s]+)/([^\r\n]+)", re.IGNORECASE)
LIMIT_DETECTION_PREFIX = "heuristics.limits.exceeded"


@dataclass(frozen=True)
class ScannerIdentity:
    backend: str
    engine_version: str | None
    database_version: str | None
    database_updated_at: datetime | None
    policy_version: str
    raw_version: str


@dataclass(frozen=True)
class ScannerHealth:
    status: str
    can_scan: bool
    message: str
    checked_at: datetime
    identity: ScannerIdentity | None = None
    definitions_age_hours: float | None = None

    def as_dict(self) -> dict[str, object]:
        identity = self.identity
        return {
            "status": self.status,
            "can_scan": self.can_scan,
            "message": self.message,
            "checked_at": self.checked_at,
            "backend": identity.backend if identity else None,
            "engine_version": identity.engine_version if identity else None,
            "database_version": identity.database_version if identity else None,
            "database_updated_at": identity.database_updated_at if identity else None,
            "definitions_age_hours": self.definitions_age_hours,
            "policy_version": identity.policy_version if identity else None,
            "raw_version": identity.raw_version if identity else None,
        }


@dataclass(frozen=True)
class ScanResult:
    clean: bool
    infected: bool
    identity: ScannerIdentity
    scan_started_at: datetime
    duration_seconds: float
    threat_name: str | None = None
    raw_output: str = ""


class ScanInterrupted(RuntimeError):
    pass


class ScannerUnavailable(RuntimeError):
    pass


class ScannerDefinitionsStale(RuntimeError):
    pass


class ScannerPolicyError(RuntimeError):
    pass


def parse_scanner_version(raw_output: str) -> tuple[str | None, str | None, datetime | None]:
    raw_output = (raw_output or "").strip().strip("\0")
    match = VERSION_PATTERN.search(raw_output)
    if not match:
        return None, None, None
    engine_version, database_version, database_date = match.groups()
    parsed_date: datetime | None = None
    try:
        parsed_date = parsedate_to_datetime(database_date.strip())
    except (TypeError, ValueError, OverflowError):
        for date_format in ("%a %b %d %H:%M:%S %Y", "%b %d %H:%M:%S %Y"):
            try:
                parsed_date = datetime.strptime(database_date.strip(), date_format)
                break
            except ValueError:
                continue
    if parsed_date and parsed_date.tzinfo is not None:
        parsed_date = parsed_date.astimezone(timezone.utc).replace(tzinfo=None)
    return engine_version, database_version, parsed_date


def parse_scan_response(response: str) -> tuple[bool, str | None]:
    response = response.strip().strip("\0")
    if not response:
        raise RuntimeError("scanner returned an empty response")
    if response.endswith(": OK") or response == "OK":
        return False, None
    if response.endswith(" FOUND"):
        suffix = response.rsplit(": ", 1)[-1]
        threat_name = suffix.removesuffix(" FOUND").strip() or "unknown"
        if threat_name.lower().startswith(LIMIT_DETECTION_PREFIX):
            raise ScannerPolicyError(
                f"ClamAV could not fully inspect this file because a configured limit was exceeded: {threat_name}"
            )
        return True, threat_name
    if response.endswith(" ERROR"):
        raise RuntimeError(f"scanner could not inspect the file: {response}")
    raise RuntimeError(f"unexpected scanner response: {response}")


class ScannerService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._health_lock = threading.Lock()
        self._cached_health: ScannerHealth | None = None
        self._cached_health_at = 0.0

    def policy_version(self) -> str:
        policy = {
            "backend": self.settings.scanner_backend,
            "max_file_bytes": self.settings.scanner_max_file_bytes,
            "policy_version": self.settings.scanner_policy_version,
        }
        if self.settings.scanner_backend == "command":
            policy["command_binary"] = self.settings.clamdscan_binary
            policy["command_args"] = self.settings.clamdscan_args
        fingerprint = hashlib.sha256(
            json.dumps(policy, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:12]
        return f"{self.settings.scanner_policy_version}:{fingerprint}"

    def scanner_version(self) -> str | None:
        health = self.health()
        return health.identity.raw_version if health.identity else None

    def health(self, *, force: bool = False) -> ScannerHealth:
        max_age = max(self.settings.scanner_health_cache_seconds, 1)
        with self._health_lock:
            if (
                not force
                and self._cached_health is not None
                and time.monotonic() - self._cached_health_at < max_age
            ):
                return self._cached_health
            health = self._load_health()
            self._cached_health = health
            self._cached_health_at = time.monotonic()
            return health

    def clear_health_cache(self) -> None:
        with self._health_lock:
            self._cached_health = None
            self._cached_health_at = 0.0

    def require_healthy(self, *, force: bool = False) -> ScannerIdentity:
        health = self.health(force=force)
        if health.can_scan and health.identity is not None:
            return health.identity
        if health.status == "stale":
            raise ScannerDefinitionsStale(health.message)
        raise ScannerUnavailable(health.message)

    def scan_path(
        self,
        path: str,
        *,
        identity: ScannerIdentity | None = None,
        heartbeat: Callable[[], bool] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> ScanResult:
        identity = identity or self.require_healthy(force=True)
        try:
            size_bytes = os.stat(path, follow_symlinks=False).st_size
        except OSError as exc:
            raise RuntimeError(f"scan file is unavailable: {path}: {exc}") from exc
        if size_bytes > self.settings.scanner_max_file_bytes:
            raise ScannerPolicyError(
                f"file is {size_bytes} bytes, above the configured ClamAV safety limit of "
                f"{self.settings.scanner_max_file_bytes} bytes: {path}"
            )
        if should_stop and should_stop():
            raise ScanInterrupted("scan interrupted before the current file started")

        started_at = datetime.utcnow()
        started = time.monotonic()
        if self.settings.scanner_backend == "clamd":
            infected, threat_name, output = self._scan_with_clamd(
                path,
                heartbeat=heartbeat,
                should_stop=should_stop,
            )
        else:
            infected, threat_name, output = self._scan_with_command(
                path,
                heartbeat=heartbeat,
                should_stop=should_stop,
            )
        return ScanResult(
            clean=not infected,
            infected=infected,
            identity=identity,
            scan_started_at=started_at,
            duration_seconds=max(time.monotonic() - started, 0.0),
            threat_name=threat_name,
            raw_output=output,
        )

    def _load_health(self) -> ScannerHealth:
        checked_at = datetime.utcnow()
        try:
            raw_version = self._version_output()
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            return ScannerHealth(
                status="unavailable",
                can_scan=False,
                message=f"ClamAV is unavailable: {exc}",
                checked_at=checked_at,
            )

        engine_version, database_version, database_updated_at = parse_scanner_version(raw_version)
        identity = ScannerIdentity(
            backend=self.settings.scanner_backend,
            engine_version=engine_version,
            database_version=database_version,
            database_updated_at=database_updated_at,
            policy_version=self.policy_version(),
            raw_version=raw_version[:255],
        )
        if not engine_version:
            return ScannerHealth(
                status="unavailable",
                can_scan=False,
                message=f"ClamAV returned an unrecognized version response: {raw_version[:180]}",
                checked_at=checked_at,
                identity=identity,
            )
        if database_updated_at is None:
            return ScannerHealth(
                status="warning",
                can_scan=False,
                message=(
                    "ClamAV is available, but signature freshness could not be verified; "
                    "new scans are blocked."
                ),
                checked_at=checked_at,
                identity=identity,
            )

        definitions_age_hours = max(
            (checked_at - database_updated_at).total_seconds() / 3600,
            0.0,
        )
        stale_hours = max(self.settings.scanner_definitions_stale_hours, 1)
        warning_hours = min(
            max(self.settings.scanner_definitions_warn_hours, 1),
            stale_hours,
        )
        if definitions_age_hours >= stale_hours:
            return ScannerHealth(
                status="stale",
                can_scan=False,
                message=(
                    f"ClamAV definitions are {definitions_age_hours:.1f} hours old; "
                    f"new scans are blocked at {stale_hours} hours."
                ),
                checked_at=checked_at,
                identity=identity,
                definitions_age_hours=definitions_age_hours,
            )
        if definitions_age_hours >= warning_hours:
            return ScannerHealth(
                status="warning",
                can_scan=True,
                message=(
                    f"ClamAV definitions are {definitions_age_hours:.1f} hours old; "
                    f"scanning will stop if they reach {stale_hours} hours."
                ),
                checked_at=checked_at,
                identity=identity,
                definitions_age_hours=definitions_age_hours,
            )
        return ScannerHealth(
            status="healthy",
            can_scan=True,
            message="ClamAV daemon and definitions are healthy.",
            checked_at=checked_at,
            identity=identity,
            definitions_age_hours=definitions_age_hours,
        )

    def _version_output(self) -> str:
        if self.settings.scanner_backend == "clamd":
            return self._clamd_request("VERSION").strip()
        proc = subprocess.run(
            [self.settings.clamdscan_binary, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if proc.returncode != 0:
            raise RuntimeError(f"version command exited {proc.returncode}: {output}")
        if not output:
            raise RuntimeError("version command returned no output")
        return output.splitlines()[0]

    def _clamd_request(self, command: str) -> str:
        path = self.settings.clamd_socket_path
        timeout = max(self.settings.scanner_connect_timeout_seconds, 1)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(timeout)
                client.connect(path)
                client.sendall(b"z" + command.encode("utf-8") + b"\0")
                chunks: list[bytes] = []
                while True:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if b"\0" in chunk:
                        break
        except OSError as exc:
            raise ScannerUnavailable(f"cannot connect to clamd socket {path}: {exc}") from exc
        output = b"".join(chunks).split(b"\0", 1)[0].decode("utf-8", errors="replace")
        if not output:
            raise ScannerUnavailable("clamd returned no response")
        return output

    def _scan_with_clamd(
        self,
        path: str,
        *,
        heartbeat: Callable[[], bool] | None,
        should_stop: Callable[[], bool] | None,
    ) -> tuple[bool, str | None, str]:
        if "\0" in path or "\n" in path or "\r" in path:
            raise ScannerPolicyError("clamd cannot safely scan a path containing a line break or NUL byte")
        socket_path = self.settings.clamd_socket_path
        interval = min(max(int(self.settings.scan_heartbeat_seconds), 1), 30)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(max(self.settings.scanner_connect_timeout_seconds, 1))
                client.connect(socket_path)
                client.sendall(b"zSCAN " + os.fsencode(path) + b"\0")
                client.settimeout(interval)
                chunks: list[bytes] = []
                while True:
                    try:
                        chunk = client.recv(4096)
                    except socket.timeout:
                        keep_running = heartbeat() if heartbeat else True
                        if (should_stop and should_stop()) or not keep_running:
                            raise ScanInterrupted("scan interrupted; the current file will be retried")
                        continue
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if b"\0" in chunk:
                        break
        except ScanInterrupted:
            raise
        except OSError as exc:
            self.clear_health_cache()
            raise ScannerUnavailable(f"lost connection to clamd socket {socket_path}: {exc}") from exc

        output = b"".join(chunks).split(b"\0", 1)[0].decode("utf-8", errors="replace")
        infected, threat_name = parse_scan_response(output)
        return infected, threat_name, output

    def _scan_with_command(
        self,
        path: str,
        *,
        heartbeat: Callable[[], bool] | None,
        should_stop: Callable[[], bool] | None,
    ) -> tuple[bool, str | None, str]:
        args = [self.settings.clamdscan_binary, *shlex.split(self.settings.clamdscan_args), path]
        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            self.clear_health_cache()
            raise ScannerUnavailable(f"failed to start scanner: {exc}") from exc

        output = ""
        interval = min(max(int(self.settings.scan_heartbeat_seconds), 1), 30)
        while True:
            try:
                stdout, _ = proc.communicate(timeout=interval)
                output = stdout or ""
                break
            except subprocess.TimeoutExpired:
                try:
                    keep_running = heartbeat() if heartbeat else True
                except Exception:
                    self._stop_process(proc)
                    raise
                if (should_stop and should_stop()) or not keep_running:
                    self._stop_process(proc)
                    raise ScanInterrupted("scan interrupted; the current file will be retried")

        limit_match = re.search(r"(Heuristics\.Limits\.Exceeded[^\s:]*)", output, re.IGNORECASE)
        if limit_match:
            raise ScannerPolicyError(
                f"ClamAV could not fully inspect this file because a configured limit was exceeded: "
                f"{limit_match.group(1)}"
            )
        if proc.returncode == 0:
            return False, None, output
        if proc.returncode == 1:
            for line in output.splitlines():
                if line.endswith(" FOUND") and ": " in line:
                    threat = line.rsplit(": ", 1)[-1].removesuffix(" FOUND").strip() or "unknown"
                    return True, threat, output
            return True, "unknown", output
        if proc.returncode in {2, 70}:
            raise RuntimeError(f"scanner failed with exit code {proc.returncode}: {output.strip()}")
        raise RuntimeError(f"scanner exited unexpectedly with code {proc.returncode}: {output.strip()}")

    @staticmethod
    def _stop_process(proc: subprocess.Popen[str]) -> None:
        if proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            proc.communicate()
            return
        except OSError:
            proc.terminate()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                proc.communicate()
                return
            except OSError:
                proc.kill()
            proc.communicate(timeout=5)
