from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

from nika_core.product_factory_packaged_execution_plan import (
    PackagedExecutionPlanAdmissionError,
    decode_packaged_product_factory_execution_plan,
)
from nika_core.product_factory_packaged_preparation import (
    PackagedProductFactoryExecutionPlan,
)

_MAX_PLAN_BYTES = 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_WINDOWS_GENERIC_READ = 0x80000000
_WINDOWS_FILE_SHARE_READ = 0x00000001
_WINDOWS_OPEN_EXISTING = 3
_WINDOWS_FILE_ATTRIBUTE_NORMAL = 0x00000080
_WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000

ExecutionPlanFileSelector = Callable[[], object]


class PackagedExecutionPlanFileError(RuntimeError):
    """Raised when an explicit execution-plan file cannot be safely admitted."""


class PackagedExecutionPlanFileResolver:
    """Resolve one ProductProject plan from one explicitly selected local JSON file.

    File selection is injected so the packaged Windows shell can reuse its incumbent
    native pywebview file dialog. This class owns only the stable file-read boundary
    and portable-plan admission; it does not infer repository or execution authority.
    """

    def __init__(self, select_file: ExecutionPlanFileSelector) -> None:
        if not callable(select_file):
            raise TypeError("execution-plan file selector must be callable")
        self._select_file = select_file

    def __call__(self, project_id: str) -> PackagedProductFactoryExecutionPlan:
        if type(project_id) is not str or not project_id:
            raise PackagedExecutionPlanFileError(
                "execution-plan resolution requires an exact ProductProject id"
            )
        selected = _selected_plan_path(self._select_file())
        payload = read_packaged_execution_plan_file(selected)
        try:
            plan = decode_packaged_product_factory_execution_plan(payload)
        except (PackagedExecutionPlanAdmissionError, TypeError) as exc:
            raise PackagedExecutionPlanFileError(
                "selected execution-plan file failed canonical admission"
            ) from exc
        if plan.project_id != project_id:
            raise PackagedExecutionPlanFileError(
                "selected execution plan belongs to another ProductProject"
            )
        return plan


def read_packaged_execution_plan_file(path: Path) -> bytes:
    """Read one bounded byte-exact plan through a held no-follow file authority."""

    candidate = Path(path)
    if not candidate.is_absolute():
        raise PackagedExecutionPlanFileError(
            "execution-plan file path must be absolute"
        )
    if candidate.suffix.casefold() != ".json":
        raise PackagedExecutionPlanFileError(
            "execution-plan file must use the .json extension"
        )

    try:
        before = os.lstat(candidate)
    except OSError as exc:
        raise PackagedExecutionPlanFileError(
            "execution-plan file is not accessible"
        ) from exc
    _require_regular_single_link(before, stage="before open")
    if before.st_size <= 0 or before.st_size > _MAX_PLAN_BYTES:
        raise PackagedExecutionPlanFileError(
            "execution-plan file must contain 1..1048576 bytes"
        )
    identity = _stable_identity(before)

    descriptor: int | None = None
    try:
        descriptor = _open_readonly_authority(candidate)
        opened = os.fstat(descriptor)
        _require_regular_single_link(opened, stage="after open")
        if _stable_identity(opened) != identity:
            _fail_changed()

        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, _READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_PLAN_BYTES:
                raise PackagedExecutionPlanFileError(
                    "execution-plan file exceeds the 1048576-byte limit"
                )
            chunks.append(chunk)

        after = os.fstat(descriptor)
        _require_regular_single_link(after, stage="after read")
        if total != opened.st_size or _stable_identity(after) != identity:
            _fail_changed()
    except PackagedExecutionPlanFileError:
        raise
    except OSError as exc:
        raise PackagedExecutionPlanFileError(
            "execution-plan file could not be read safely"
        ) from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass

    try:
        current = os.lstat(candidate)
    except OSError as exc:
        raise PackagedExecutionPlanFileError(
            "execution-plan file changed during read"
        ) from exc
    _require_regular_single_link(current, stage="after read")
    if _stable_identity(current) != identity:
        _fail_changed()
    return b"".join(chunks)


def _selected_plan_path(selection: object) -> Path:
    if selection is None:
        raise PackagedExecutionPlanFileError(
            "execution-plan file selection was cancelled"
        )
    if type(selection) is not tuple or len(selection) != 1:
        raise PackagedExecutionPlanFileError(
            "select exactly one execution-plan JSON file"
        )
    value = selection[0]
    if type(value) is not str or not value or "\x00" in value:
        raise PackagedExecutionPlanFileError(
            "execution-plan file selection is invalid"
        )
    path = Path(value)
    if not path.is_absolute():
        raise PackagedExecutionPlanFileError(
            "execution-plan file selection must be an absolute path"
        )
    return path


def _open_readonly_authority(path: Path) -> int:
    if os.name == "nt":
        try:
            import ctypes
            import msvcrt
        except ImportError as exc:
            raise OSError(
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
            raise OSError(ctypes.get_last_error(), "CreateFileW failed")
        try:
            return msvcrt.open_osfhandle(
                int(handle),
                os.O_RDONLY | int(getattr(os, "O_BINARY", 0)),
            )
        except (OSError, OverflowError, ValueError):
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [ctypes.c_void_p]
            close_handle.restype = ctypes.c_int
            close_handle(ctypes.c_void_p(handle))
            raise

    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("platform does not provide no-follow file opens")
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0)) | int(nofollow)
    flags |= int(getattr(os, "O_NONBLOCK", 0))
    return os.open(path, flags)


def _require_regular_single_link(value: os.stat_result, *, stage: str) -> None:
    if (
        not stat.S_ISREG(value.st_mode)
        or _is_reparse(value)
        or value.st_nlink != 1
    ):
        raise PackagedExecutionPlanFileError(
            f"execution-plan file is not one regular unlinked authority ({stage})"
        )


def _is_reparse(value: os.stat_result) -> bool:
    attributes = int(getattr(value, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_flag)


def _stable_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _fail_changed() -> NoReturn:
    raise PackagedExecutionPlanFileError(
        "execution-plan file changed during stable read"
    )
