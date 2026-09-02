#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path

from clamd_healthcheck import definitions_ready

BASE_CONFIG_PATH = Path("/etc/clamav/clamd.conf")
RUNTIME_CONFIG_PATH = Path("/tmp/clamav/clamd.runtime.conf")
DEFAULT_MAX_SCAN_SIZE_MIB = 2000
MAX_MAX_SCAN_SIZE_MIB = 4000
MAX_SCAN_SIZE_PATTERN = re.compile(
    r"^(?P<prefix>[ \t]*MaxScanSize[ \t]+)(?P<value>\S+)"
    r"(?P<suffix>[ \t]*(?:#.*)?)$",
    re.MULTILINE,
)


def configured_max_scan_size_mib(
    environment: Mapping[str, str] | None = None,
) -> int:
    values = os.environ if environment is None else environment
    raw_value = values.get(
        "CLAMD_MAX_SCAN_SIZE_MIB", str(DEFAULT_MAX_SCAN_SIZE_MIB)
    ).strip()
    try:
        value = int(raw_value, 10)
    except ValueError as exc:
        raise RuntimeError(
            "CLAMD_MAX_SCAN_SIZE_MIB must be a whole number between "
            f"1 and {MAX_MAX_SCAN_SIZE_MIB}"
        ) from exc
    if not 1 <= value <= MAX_MAX_SCAN_SIZE_MIB:
        raise RuntimeError(
            "CLAMD_MAX_SCAN_SIZE_MIB must be between "
            f"1 and {MAX_MAX_SCAN_SIZE_MIB}; received {raw_value!r}"
        )
    return value


def render_runtime_config(base_config: str, max_scan_size_mib: int) -> str:
    matches = list(MAX_SCAN_SIZE_PATTERN.finditer(base_config))
    if len(matches) != 1:
        raise RuntimeError(
            "base ClamD configuration must contain exactly one active "
            f"MaxScanSize directive; found {len(matches)}"
        )
    return MAX_SCAN_SIZE_PATTERN.sub(
        lambda match: (
            f"{match.group('prefix')}{max_scan_size_mib}M{match.group('suffix')}"
        ),
        base_config,
        count=1,
    )


def write_runtime_config(
    base_path: Path = BASE_CONFIG_PATH,
    runtime_path: Path = RUNTIME_CONFIG_PATH,
    environment: Mapping[str, str] | None = None,
) -> tuple[Path, int]:
    max_scan_size_mib = configured_max_scan_size_mib(environment)
    try:
        base_config = base_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"cannot read base ClamD configuration: {exc}") from exc
    runtime_config = render_runtime_config(base_config, max_scan_size_mib)

    runtime_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{runtime_path.name}.",
        suffix=".tmp",
        dir=runtime_path.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(runtime_config)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.chmod(0o600)
        os.replace(temporary_path, runtime_path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return runtime_path, max_scan_size_mib


def main() -> int:
    # Compose mounts /tmp as tmpfs, so recreate the private log directory hidden
    # by that mount before clamd opens its configured log file.
    log_directory = Path("/tmp/clamav")
    log_directory.mkdir(mode=0o700, exist_ok=True)
    log_path = log_directory / "clamd.log"
    log_path.touch(mode=0o640, exist_ok=True)
    try:
        runtime_config, max_scan_size_mib = write_runtime_config()
    except (OSError, RuntimeError) as exc:
        print(f"invalid ClamD configuration: {exc}", file=sys.stderr)
        return 2
    print(
        f"configured ClamD MaxScanSize={max_scan_size_mib}M "
        "(MaxFileSize and StreamMaxLength remain 2000M)",
        flush=True,
    )
    subprocess.Popen(["tail", "-n", "0", "-F", str(log_path)], close_fds=True)
    timeout = max(int(os.environ.get("DEFINITIONS_WAIT_TIMEOUT", "1800")), 1)
    deadline = time.monotonic() + timeout
    last_error = "definitions are not ready"
    while time.monotonic() < deadline:
        try:
            daily, age = definitions_ready()
            print(f"definitions ready: daily={daily.name} age={age}s", flush=True)
            os.execvp("clamd", ["clamd", f"--config-file={runtime_config}"])
        except (OSError, RuntimeError) as exc:
            last_error = str(exc)
        time.sleep(min(2, max(0.1, deadline - time.monotonic())))
    print(f"definitions did not become ready within {timeout}s: {last_error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
