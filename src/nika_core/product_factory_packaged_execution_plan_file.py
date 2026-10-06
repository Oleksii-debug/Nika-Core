from __future__ import annotations

import logging
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from threading import Lock
from typing import Any

from nika_core.product_factory_packaged_execution_plan import (
    PackagedExecutionPlanAdmissionError,
    decode_packaged_product_factory_execution_plan,
)
from nika_core.product_factory_packaged_preparation import (
    PackagedProductFactoryExecutionPlan,
)
from nika_core.ui.bridge_models import UIResult

_LOGGER = logging.getLogger(__name__)
_MAX_PLAN_BYTES = 1024 * 1024
_READ_CHUNK = 64 * 1024
_MAX_PATH_CHARS = 32_767
_REPARSE_POINT = 0x400

_WINDOWS_GENERIC_READ = 0x80000000
_WINDOWS_FILE_SHARE_READ = 0x00000001
_WINDOWS_OPEN_EXISTING = 3
_WINDOWS_FILE_ATTRIBUTE_NORMAL = 0x00000080
_WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000

_FileIdentity = tuple[int, int, int, int, int, int]


class PackagedExecutionPlanFileError(ValueError):
    """Raised when a selected execution-plan file is not safe to use."""


class PackagedProductFactoryExecutionPlanFileSource:
    """In-memory authority for one explicitly selected packaged execution plan.

    The selected pathname is never persisted or projected. File bytes are admitted only
    after a held-descriptor, no-follow/reparse-safe bounded read and are then decoded by
    the incumbent strict execution-plan JSON boundary.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._plan: PackagedProductFactoryExecutionPlan | None = None

    def load(self, payload: Mapping[str, Any]) -> UIResult:
        """Replace the in-memory plan with one explicitly selected safe file."""

        with self._lock:
            self._plan = None

        try:
            path = _selected_path(payload)
        except (PackagedExecutionPlanFileError, OSError, ValueError) as exc:
            _log_failure("path admission", exc)
            return _result(
                "rejected",
                "Вкажіть повний безпечний шлях до JSON-плану Product Factory.",
            )

        try:
            encoded = _read_stable_plan_bytes(path)
        except (PackagedExecutionPlanFileError, OSError) as exc:
            _log_failure("file snapshot", exc)
            return _result(
                "failed",
                "Не вдалося безпечно прочитати вибраний JSON-план Product Factory.",
            )

        try:
            plan = decode_packaged_product_factory_execution_plan(encoded)
        except (PackagedExecutionPlanAdmissionError, TypeError, ValueError) as exc:
            _log_failure("JSON admission", exc)
            return _result(
                "rejected",
                "Вибраний файл не є допустимим JSON-планом Product Factory.",
            )

        with self._lock:
            self._plan = plan
        return _result(
            "completed",
            f"План виконання Product Factory завантажено для {plan.project_id}.",
        )

    def resolve(self, project_id: str) -> PackagedProductFactoryExecutionPlan:
        """Return the loaded plan only when it belongs to the exact selected project."""

        if type(project_id) is not str or not project_id:
            raise PackagedExecutionPlanFileError(
                "ProductProject identity must be non-empty exact text"
            )
        with self._lock:
            plan = self._plan
        if plan is None:
            raise PackagedExecutionPlanFileError(
                "no packaged Product Factory execution plan is loaded"
            )
        if plan.project_id != project_id:
            raise PackagedExecutionPlanFileError(
                "loaded execution plan belongs to another ProductProject"
            )
        return plan

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            plan = self._plan
        if plan is None:
            return {
                "status": "missing",
                "loaded": False,
                "project_id": None,
                "message": "JSON-план виконання Product Factory ще не завантажено.",
            }
        return {
            "status": "loaded",
            "loaded": True,
            "project_id": plan.project_id,
            "message": (
                "JSON-план виконання Product Factory завантажено для "
                f"{plan.project_id}."
            ),
        }


def _selected_path(payload: Mapping[str, Any]) -> Path:
    if type(payload) is not dict or set(payload) != {"path"}:
        raise PackagedExecutionPlanFileError(
            "execution-plan file payload must contain only path"
        )
    raw_path = payload["path"]
    if type(raw_path) is not str:
        raise PackagedExecutionPlanFileError("execution-plan path must be exact text")
    if (
        not raw_path
        or raw_path != raw_path.strip()
        or len(raw_path) > _MAX_PATH_CHARS
        or "\x00" in raw_path
        or any(ord(character) < 32 for character in raw_path)
    ):
        raise PackagedExecutionPlanFileError("execution-plan path is malformed")

    path = Path(raw_path)
    if not path.is_absolute():
        raise PackagedExecutionPlanFileError("execution-plan path must be absolute")
    return path


def _read_stable_plan_bytes(path: Path) -> bytes:
    before = _require_direct_file(path)
    descriptor = _open_read_authority(path)
    try:
        opened = os.fstat(descriptor)
        _require_open_identity(before, opened)

        first = _read_held_bytes(descriptor)
        middle = os.fstat(descriptor)
        _require_open_identity(before, middle)

        second = _read_held_bytes(descriptor)
        after = os.fstat(descriptor)
        current = _require_direct_file(path)

        identities = (
            _identity(before),
            _identity(opened),
            _identity(middle),
            _identity(after),
            _identity(current),
        )
        if len(set(identities)) != 1:
            raise PackagedExecutionPlanFileError(
                "execution-plan file identity changed while reading"
            )
        if first != second or len(first) != before.st_size:
            raise PackagedExecutionPlanFileError(
                "execution-plan file bytes changed while reading"
            )
        return first
    finally:
        os.close(descriptor)


def _require_direct_file(path: Path) -> os.stat_result:
    try:
        evidence = os.lstat(path)
    except OSError as exc:
        raise PackagedExecutionPlanFileError(
            "execution-plan file is unavailable"
        ) from exc
    if (
        not stat.S_ISREG(evidence.st_mode)
        or stat.S_ISLNK(evidence.st_mode)
        or _is_reparse(evidence)
        or evidence.st_nlink != 1
        or evidence.st_size <= 0
        or evidence.st_size > _MAX_PLAN_BYTES
    ):
        raise PackagedExecutionPlanFileError(
            "execution-plan file must be one direct bounded regular file"
        )
    return evidence


def _require_open_identity(
    expected: os.stat_result,
    opened: os.stat_result,
) -> None:
    if (
        not stat.S_ISREG(opened.st_mode)
        or _is_reparse(opened)
        or opened.st_nlink != 1
        or _identity(opened) != _identity(expected)
    ):
        raise PackagedExecutionPlanFileError(
            "execution-plan file identity changed during open"
        )


def _read_held_bytes(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    total = 0
    while True:
        remaining = _MAX_PLAN_BYTES + 1 - total
        if remaining <= 0:
            raise PackagedExecutionPlanFileError(
                "execution-plan file exceeds the byte limit"
            )
        chunk = os.read(descriptor, min(_READ_CHUNK, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    payload = b"".join(chunks)
    if not payload or len(payload) > _MAX_PLAN_BYTES:
        raise PackagedExecutionPlanFileError(
            "execution-plan file size is invalid"
        )
    return payload


def _open_read_authority(path: Path) -> int:
    if os.name == "nt":
        return _open_windows_read_authority(path)

    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise PackagedExecutionPlanFileError(
            "platform does not provide no-follow execution-plan file opens"
        )
    flags = os.O_RDONLY
    flags |= int(getattr(os, "O_BINARY", 0))
    flags |= int(nofollow)
    flags |= int(getattr(os, "O_NONBLOCK", 0))
    try:
        return os.open(path, flags)
    except OSError as exc:
        raise PackagedExecutionPlanFileError(
            "execution-plan file could not be opened safely"
        ) from exc


def _open_windows_read_authority(path: Path) -> int:
    try:
        import ctypes
        import msvcrt
    except ImportError as exc:
        raise PackagedExecutionPlanFileError(
            "Windows execution-plan file authority is unavailable"
        ) from exc

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        os.fspath(path),
        _WINDOWS_GENERIC_READ,
        _WINDOWS_FILE_SHARE_READ,
        None,
        _WINDOWS_OPEN_EXISTING,
        _WINDOWS_FILE_ATTRIBUTE_NORMAL | _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle is None or handle == invalid_handle:
        raise PackagedExecutionPlanFileError(
            "execution-plan file could not be opened safely"
        )

    try:
        return msvcrt.open_osfhandle(
            int(handle),
            os.O_RDONLY | int(getattr(os, "O_BINARY", 0)),
        )
    except (OSError, OverflowError, ValueError) as exc:
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int
        close_handle(ctypes.c_void_p(handle))
        raise PackagedExecutionPlanFileError(
            "execution-plan Windows handle could not be adopted"
        ) from exc


def _identity(value: os.stat_result) -> _FileIdentity:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(getattr(value, "st_ctime_ns", 0)),
        int(value.st_nlink),
    )


def _is_reparse(value: os.stat_result) -> bool:
    attributes = int(getattr(value, "st_file_attributes", 0))
    return bool(attributes & _REPARSE_POINT)


def _result(status: str, message: str) -> UIResult:
    return UIResult(
        request_id="desktop-handler",
        status=status,
        message=message,
        focus_id="product-factory-execution-plan-path",
    )


def _log_failure(stage: str, exc: Exception) -> None:
    _LOGGER.error(
        "Packaged Product Factory execution-plan %s failed: exception_type=%s",
        stage,
        type(exc).__name__,
    )