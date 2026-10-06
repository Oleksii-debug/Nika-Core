from __future__ import annotations

import os
import pathlib
import re
import stat
from dataclasses import dataclass

from nika_core.product_factory_packaged_execution_plan import (
    PackagedExecutionPlanAdmissionError,
    decode_packaged_product_factory_execution_plan,
)
from nika_core.product_factory_packaged_preparation import (
    PackagedProductFactoryExecutionPlan,
)
from nika_core.toolsmith.workspace_security import (
    WorkspaceSecurityError,
    ensure_real_directory_root,
)

_PROJECT_ID = re.compile(r"product-[0-9a-f]{64}")
_MAX_PLAN_BYTES = 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024


class PackagedExecutionPlanFileError(RuntimeError):
    """Raised when an operator-supplied plan file is not a stable authority."""


@dataclass(frozen=True, slots=True)
class _DirectoryIdentity:
    device: int
    inode: int
    mode: int


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    link_count: int


class PackagedExecutionPlanFileResolver:
    """Resolve one selected ProductProject to a stable explicit plan file.

    The directory is a trusted operator boundary. File names are derived only from
    canonical ProductProject ids; repository graph, base SHA, component goals and
    permissions still come from the strict packaged-plan decoder. This resolver
    never infers execution authority from user prose and never reads credentials.
    """

    def __init__(self, directory: pathlib.Path) -> None:
        try:
            resolved = ensure_real_directory_root(
                pathlib.Path(directory),
                label="Product Factory execution-plan directory",
            )
        except (OSError, WorkspaceSecurityError, ValueError) as exc:
            raise PackagedExecutionPlanFileError(
                "Product Factory execution-plan directory is unsafe"
            ) from exc
        try:
            directory_stat = resolved.stat()
        except OSError as exc:
            raise PackagedExecutionPlanFileError(
                "Product Factory execution-plan directory is unavailable"
            ) from exc
        self._directory = resolved
        self._directory_identity = _directory_identity(directory_stat)

    def __call__(self, project_id: str) -> PackagedProductFactoryExecutionPlan:
        identity = _canonical_project_id(project_id)
        self._require_directory_authority()

        plan_path = self._directory / f"{identity}.json"
        payload, file_identity = _read_stable_plan_file(plan_path)
        try:
            plan = decode_packaged_product_factory_execution_plan(payload)
        except PackagedExecutionPlanAdmissionError as exc:
            raise PackagedExecutionPlanFileError(
                "Product Factory execution-plan file was rejected"
            ) from exc

        if plan.project_id != identity:
            raise PackagedExecutionPlanFileError(
                "Product Factory execution-plan file belongs to another ProductProject"
            )

        self._require_directory_authority()
        _require_unchanged_plan_path(plan_path, file_identity)
        return plan

    def _require_directory_authority(self) -> None:
        try:
            resolved = ensure_real_directory_root(
                self._directory,
                label="Product Factory execution-plan directory",
            )
            current = resolved.stat()
        except (OSError, WorkspaceSecurityError, ValueError) as exc:
            raise PackagedExecutionPlanFileError(
                "Product Factory execution-plan directory changed"
            ) from exc
        current_identity = _directory_identity(current)\n        if resolved != self._directory or current_identity != self._directory_identity:
            raise PackagedExecutionPlanFileError(
                "Product Factory execution-plan directory identity changed"
            )


def _canonical_project_id(value: object) -> str:
    if type(value) is not str or _PROJECT_ID.fullmatch(value) is None:
        raise PackagedExecutionPlanFileError(
            "execution-plan resolution requires a canonical ProductProject id"
        )
    return value


def _directory_identity(value: os.stat_result) -> _DirectoryIdentity:
    return _DirectoryIdentity(
        device=value.st_dev,
        inode=value.st_ino,
        mode=stat.S_IFMT(value.st_mode),
    )


def _file_identity(value: os.stat_result) -> _FileIdentity:
    return _FileIdentity(
        device=value.st_dev,
        inode=value.st_ino,
        mode=value.st_mode,
        size=value.st_size,
        mtime_ns=value.st_mtime_ns,
        ctime_ns=value.st_ctime_ns,
        link_count=value.st_nlink,
    )


def _is_reparse(value: os.stat_result) -> bool:
    attributes = getattr(value, "st_file_attributes", 0)
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(flag and attributes & flag)


def _require_regular_single_link(value: os.stat_result) -> None:
    if (
        not stat.S_ISREG(value.st_mode)
        or stat.S_ISLNK(value.st_mode)
        or _is_reparse(value)
    ):
        raise PackagedExecutionPlanFileError(
            "Product Factory execution-plan path must be a regular non-reparse file"
        )
    if value.st_nlink != 1:
        raise PackagedExecutionPlanFileError(
            "Product Factory execution-plan file must have one pathname authority"
        )


def _lstat_plan(path: pathlib.Path) -> os.stat_result:
    try:
        value = path.lstat()
    except FileNotFoundError as exc:
        raise PackagedExecutionPlanFileError(
            "Product Factory execution-plan file is missing"
        ) from exc
    except OSError as exc:
        raise PackagedExecutionPlanFileError(
            "Product Factory execution-plan file cannot be inspected"
        ) from exc
    _require_regular_single_link(value)
    return value


def _read_stable_plan_file(path: pathlib.Path) -> tuple[bytes, _FileIdentity]:
    before = _lstat_plan(path)
    before_identity = _file_identity(before)
    if before.st_size > _MAX_PLAN_BYTES:
        raise PackagedExecutionPlanFileError(
            "Product Factory execution-plan file exceeds the byte limit"
        )

    flags = os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOINHERIT", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PackagedExecutionPlanFileError(
            "Product Factory execution-plan file cannot be opened safely"
        ) from exc

    try:
        opened = os.fstat(descriptor)
        _require_regular_single_link(opened)
        if _file_identity(opened) != before_identity:
            raise PackagedExecutionPlanFileError(
                "Product Factory execution-plan identity changed before read"
            )

        chunks: list[bytes] = []
        total = 0
        while True:
            remaining = _MAX_PLAN_BYTES + 1 - total\n            chunk = os.read(descriptor, min(_READ_CHUNK_BYTES, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_PLAN_BYTES:
                raise PackagedExecutionPlanFileError(
                    "Product Factory execution-plan file exceeds the byte limit"
                )
        payload = b"".join(chunks)

        held_after = os.fstat(descriptor)
        _require_regular_single_link(held_after)
        if _file_identity(held_after) != before_identity:
            raise PackagedExecutionPlanFileError(
                "Product Factory execution-plan file changed during read"
            )
    finally:
        os.close(descriptor)

    _require_unchanged_plan_path(path, before_identity)
    return payload, before_identity


def _require_unchanged_plan_path(
    path: pathlib.Path,
    expected: _FileIdentity,
) -> None:
    current = _lstat_plan(path)
    if _file_identity(current) != expected:
        raise PackagedExecutionPlanFileError(
            "Product Factory execution-plan path changed during admission"
        )
