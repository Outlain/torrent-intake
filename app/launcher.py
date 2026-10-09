"""One controller per local data directory; offline restore before opening SQLite."""
import fcntl
import logging
import os
import sys

from .config import Settings, persist_settings
from .archive_tools import ArchiveError, cleanup_archive_workspaces
from .restore import apply_pending_restore
from .state_files import data_directory


def main() -> None:
    root = data_directory()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.umask(0o077)
    descriptor = os.open(root / "controller.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Another Torrent Intake controller is already using this data directory")
    # Retain the advisory lock across exec for the entire server lifetime.
    os.set_inheritable(descriptor, True)
    apply_pending_restore(Settings())
    settings = Settings()
    if settings.archive_enabled:
        try:
            cleanup_archive_workspaces(str(root), settings.archive_scratch_dir, settings.archive_scratch_mount_marker)
        except (ArchiveError, OSError) as exc:
            # An offline NAS should hold archive scans, not prevent access to
            # the UI or unrelated native/media jobs. Every scan rechecks it.
            logging.getLogger(__name__).warning("Archive scratch cleanup deferred: %s", exc)
    persist_settings(settings)
    (root / "restart-required").unlink(missing_ok=True)
    command = sys.argv[1:]
    if not command:
        raise SystemExit("No application command supplied")
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
