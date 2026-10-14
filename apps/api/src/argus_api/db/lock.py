import fcntl
import os
from pathlib import Path
from types import TracebackType


class AlreadyRunningError(RuntimeError):
    pass


class DataDirLock:
    """Exclusive lock on the data directory: a second Argus on the same data is a bug."""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "argus.lock"
        self._fd: int | None = None

    def acquire(self) -> None:
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise AlreadyRunningError(
                f"another Argus process holds {self.path}"
            ) from None
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)  # closing the descriptor drops the flock
            self._fd = None

    def __enter__(self) -> "DataDirLock":
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
