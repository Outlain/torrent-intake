from __future__ import annotations
import os
import shlex
import signal
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from .config import get_settings


@dataclass
class ScanResult:
    clean: bool
    infected: bool
    threat_name: str | None = None
    raw_output: str = ""


class ScanInterrupted(RuntimeError):
    pass


class ScannerService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def scanner_version(self) -> str | None:
        try:
            proc = subprocess.run(
                [self.settings.clamdscan_binary, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return output.splitlines()[0][:255] if output else None

    def scan_path(
        self,
        path: str,
        *,
        heartbeat: Callable[[], bool] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> ScanResult:
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
            raise RuntimeError(f"failed to start scanner: {exc}") from exc

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

        if proc.returncode == 0:
            return ScanResult(clean=True, infected=False, raw_output=output)

        if proc.returncode == 1:
            threat = None
            for line in output.splitlines():
                if line.endswith(" FOUND") and ": " in line:
                    try:
                        _, suffix = line.rsplit(": ", 1)
                        threat = suffix.removesuffix(" FOUND").strip()
                        break
                    except ValueError:
                        continue
            return ScanResult(clean=False, infected=True, threat_name=threat, raw_output=output)

        raise RuntimeError(f"scanner failed with exit code {proc.returncode}: {output.strip()}")

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
