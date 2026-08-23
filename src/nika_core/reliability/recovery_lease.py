from __future__ import annotations

import errno
import os
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class RecoveryLeaseError(RuntimeError):
    """Recovery ownership or SQLite quiescence could not be established safely."""


class RecoveryLeaseBusyError(RecoveryLeaseError):
    """Another process or SQLite client currently owns incompatible access."""


class RecoveryFileLease:
    """Small cross-process advisory lease for one authoritative SQLite path.

    The sibling lock file is intentionally persistent. Removing a file-backed lock on
    release can split ownership across two inodes when another process opens the old
    inode while a replacement lock file is created.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    def __enter__(self) -> RecoveryFileLease:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._reject_indirect_lock_path()
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if nofollow:
            flags |= nofollow
        try:
            fd = os.open(self._path, flags, 0o600)
        except OSError as exc:
            raise RecoveryLeaseError("SQLite recovery lease file cannot be opened safely") from exc
        self._fd = fd
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise RecoveryLeaseError("SQLite recovery lease is not a regular file")
            self._reject_indirect_lock_path()
            current = os.stat(self._path)
            if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                raise RecoveryLeaseError("SQLite recovery lease path changed while opening")
            if opened.st_size == 0:
                os.write(fd, b"\x00")
                os.fsync(fd)
            self._lock(fd)
        except BaseException:
            os.close(fd)
            self._fd = None
            raise
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        fd = self._fd
        self._fd = None
        if fd is None:
            return
        try:
            self._unlock(fd)
        finally:
            os.close(fd)

    def _reject_indirect_lock_path(self) -> None:
        try:
            info = os.lstat(self._path)
        except FileNotFoundError:
            return
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        file_attributes = getattr(info, "st_file_attributes", 0)
        if stat.S_ISLNK(info.st_mode) or bool(file_attributes & reparse_flag):
            raise RecoveryLeaseError("SQLite recovery lease path must not be indirect")
        if not stat.S_ISREG(info.st_mode):
            raise RecoveryLeaseError("SQLite recovery lease path is not a regular file")

    @staticmethod
    def _lock(fd: int) -> None:
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                raise RecoveryLeaseBusyError(
                    "another SQLite recovery operation owns the recovery lease"
                ) from exc
            raise RecoveryLeaseError("SQLite recovery lease acquisition failed") from exc

    @staticmethod
    def _unlock(fd: int) -> None:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def exclusive_sqlite_lease(path: Path) -> Iterator[sqlite3.Connection]:
    """Hold SQLite's native EXCLUSIVE locking mode until the connection closes.

    This is intentionally separate from the recovery file lease. The file lease
    serializes recovery owners even for missing/corrupt targets; SQLite's own lock is
    the quiescence authority for a healthy live database and is automatically obeyed by
    ordinary SQLite clients in other processes.
    """

    try:
        connection = sqlite3.connect(path, timeout=0.0, isolation_level=None)
    except sqlite3.Error as exc:
        raise RecoveryLeaseError("live SQLite database cannot be opened for recovery") from exc
    try:
        mode_row = connection.execute("PRAGMA locking_mode = EXCLUSIVE").fetchone()
        mode = str(mode_row[0]).casefold() if mode_row else ""
        if mode != "exclusive":
            raise RecoveryLeaseError("SQLite refused exclusive recovery locking mode")
        try:
            connection.execute("BEGIN EXCLUSIVE")
            connection.execute("SELECT count(*) FROM sqlite_schema").fetchone()
            connection.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            if "locked" in str(exc).casefold() or "busy" in str(exc).casefold():
                raise RecoveryLeaseBusyError(
                    "live SQLite clients prevent exclusive recovery ownership"
                ) from exc
            raise RecoveryLeaseError("SQLite recovery quiescence probe failed") from exc
        except sqlite3.DatabaseError as exc:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise RecoveryLeaseError("live database is not valid SQLite recovery state") from exc
        yield connection
    finally:
        connection.close()
