"""Cooperative whole-controller pause and local administrator credentials."""
from __future__ import annotations

import asyncio
import hmac
from pathlib import Path
import secrets
import threading

from .config import Settings
from .state_files import read_private, sync_directory, write_json, write_private
from .worker import worker_loop


class Controller:
    def __init__(self, settings: Settings, *, fresh: bool):
        self.root = Path(settings.data_dir)
        self.pause_path = self.root / "controller-paused.json"
        self.paused = fresh or self.pause_path.exists()
        self.reason = "New installation: configure or restore before resuming" if fresh else "Controller paused by operator"
        if self.pause_path.exists():
            import json
            self.reason = json.loads(read_private(self.pause_path)).get("reason", self.reason)
        if self.paused:
            write_json(self.pause_path, {"reason": self.reason})
        token_path = self.root / "admin-token"
        try:
            self.token = read_private(token_path, 256).decode().strip()
        except FileNotFoundError:
            self.token = secrets.token_urlsafe(32)
            write_private(token_path, (self.token + "\n").encode())
        if len(self.token) < 32:
            raise ValueError("admin-token is invalid; it must contain at least 32 characters")
        token_path.chmod(0o600)
        self.task: asyncio.Task | None = None
        self.stop = asyncio.Event()
        self.scan_stop = threading.Event()
        self.active_mutations = 0
        self.operation_lock = asyncio.Lock()

    def authorized(self, supplied: str) -> bool:
        return hmac.compare_digest(supplied.encode(), self.token.encode())

    def status(self) -> dict:
        return {
            "paused": self.paused, "reason": self.reason if self.paused else None,
            "drained": self.paused and (self.task is None or self.task.done()) and self.active_mutations == 0,
            "restart_required": (self.root / "restart-required").exists() or (self.root / ".restore-pending").exists(),
            "restore_pending": (self.root / ".restore-pending").exists(),
        }

    def pause(self, reason: str = "Operator requested a portable backup or configuration change") -> dict:
        write_json(self.pause_path, {"reason": reason})
        self.paused, self.reason = True, reason
        self.stop.set()
        self.scan_stop.set()
        return self.status()

    def require_drained(self) -> None:
        state = self.status()
        if not state["drained"]:
            raise ValueError("Pause the controller and wait until it is drained first")
        if state["restart_required"]:
            raise ValueError("Restart the container to apply the pending settings or restore first")

    def start(self) -> None:
        if self.task is not None and not self.task.done():
            raise ValueError("The previous worker is still draining")
        self.stop, self.scan_stop = asyncio.Event(), threading.Event()
        self.task = asyncio.create_task(worker_loop(self.stop, self.scan_stop))

    def resume(self) -> dict:
        self.require_drained()
        self.pause_path.unlink(missing_ok=True)
        sync_directory(self.root)
        self.paused, self.reason = False, ""
        self.start()
        return self.status()

    async def shutdown(self) -> None:
        # A normal shutdown is restart-recoverable, not a persistent operator pause.
        self.stop.set()
        self.scan_stop.set()
        if self.task:
            await self.task
