"""Bounded FFmpeg/FFprobe processes; no preexec_fn in the threaded scanner."""
from __future__ import annotations

import math
import os
import resource
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence


MAX_STDOUT_BYTES = 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
MAX_ADDRESS_SPACE_BYTES = 512 * 1024 * 1024
MAX_SINGLE_ALLOCATION_BYTES = 64 * 1024 * 1024


class MediaToolError(RuntimeError):
    pass


def run_media_tool(
    command: Sequence[str],
    *,
    descriptor: int,
    deadline: float,
    max_file_bytes: int,
    check_active: Callable[[], None],
    cwd: str | None = None,
    max_stdout_bytes: int = MAX_STDOUT_BYTES,
) -> subprocess.CompletedProcess[bytes]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise MediaToolError("media inspection timed out")
    check_active()
    # Apply process-local limits before exec, not in a thread-unsafe fork hook.
    wrapped = [
        sys.executable, os.path.abspath(__file__), str(max_file_bytes),
        str(max(1, math.ceil(remaining))), *command,
    ]
    with subprocess.Popen(
        wrapped, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, pass_fds=(descriptor,), start_new_session=True,
        cwd=cwd, bufsize=0,
    ) as process:
        output = {"stdout": bytearray(), "stderr": bytearray()}
        limits = {"stdout": max_stdout_bytes, "stderr": MAX_STDERR_BYTES}
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, "stdout")
                selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                next_check = time.monotonic()
                while selector.get_map() or process.poll() is None:
                    now = time.monotonic()
                    if now >= deadline:
                        raise MediaToolError("media inspection timed out")
                    if now >= next_check:
                        check_active()
                        next_check = now + 1
                    for key, _ in selector.select(timeout=min(0.2, deadline - now)):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        name = key.data
                        if len(output[name]) + len(chunk) > limits[name]:
                            raise MediaToolError(f"media inspection exceeded its {name} output limit")
                        output[name].extend(chunk)
                returncode = process.wait()
        except BaseException:
            # Reap the process before callers remove its temporary attachments.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise
    return subprocess.CompletedProcess(command, returncode, bytes(output["stdout"]), bytes(output["stderr"]))


def main() -> None:
    file_limit, cpu_limit = map(int, sys.argv[1:3])
    resource.setrlimit(resource.RLIMIT_AS, (MAX_ADDRESS_SPACE_BYTES, MAX_ADDRESS_SPACE_BYTES))
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_limit, file_limit))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.execv(sys.argv[3], sys.argv[3:])


if __name__ == "__main__":
    main()
