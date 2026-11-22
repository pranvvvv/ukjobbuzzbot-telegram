"""Single-instance guard.

Two JobPilot processes on one machine would both run the scheduler and send duplicate
digests before Telegram's 409 Conflict ever surfaces, so we take an OS-level file lock
first. (Telegram conflicts across machines are handled in `jobpilot.bot.runner`.)
"""

from __future__ import annotations

from pathlib import Path
from types import TracebackType

from filelock import FileLock, Timeout

from jobpilot.core.errors import AlreadyRunningError


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = FileLock(str(path), timeout=0)

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._lock.acquire()
        except Timeout:
            raise AlreadyRunningError(
                "JobPilot is already running on this computer "
                f"(lock held: {self._path}). Stop the other instance first."
            ) from None

    def release(self) -> None:
        if self._lock.is_locked:
            self._lock.release()

    @property
    def is_locked(self) -> bool:
        return self._lock.is_locked

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def lock_is_free(path: Path) -> bool:
    """Probe without keeping the lock (used by `doctor`)."""
    probe = InstanceLock(path)
    try:
        probe.acquire()
    except AlreadyRunningError:
        return False
    probe.release()
    return True
