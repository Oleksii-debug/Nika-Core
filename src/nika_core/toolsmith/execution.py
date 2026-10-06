from __future__ import annotations

import collections.abc
import dataclasses
import hashlib
import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
import threading
import time

from nika_core.process_containment import (
    ProcessContainmentError,
    WindowsJob as _WindowsJob,
    process_group_popen_options,
    terminate_process_tree as _terminate_process_tree,
)
from nika_core.toolsmith import contracts as toolsmith_contracts
from nika_core.toolsmith.workspace_security import (
    SterileGitPlan,
    TreeEvidence,
    WorkspacePathPolicy,
    WorkspaceSecurityError,
    assert_cleanup_tree_safe,
    collect_tree_evidence,
    ensure_path_policy,
    ensure_real_directory_root,
    sterile_process_environment,
    validate_typed_argv,
)


_WINDOWS_GENERIC_READ = 0x80000000
_WINDOWS_FILE_SHARE_READ = 0x00000001
_WINDOWS_FILE_SHARE_WRITE = 0x00000002
_WINDOWS_OPEN_EXISTING = 3
_WINDOWS_FILE_ATTRIBUTE_NORMAL = 0x00000080
_WINDOWS_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000


class ProcessExecutionError(RuntimeError):
    """Raised when a typed process cannot be executed within the declared policy."""


@dataclasses.dataclass(frozen=True, slots=True)
class ProcessExecutionResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    cancelled: bool
    output_limit_exceeded: bool
    isolation_class: toolsmith_contracts.IsolationClass


@dataclasses.dataclass(frozen=True, slots=True)
class PreparedGitWorkspace:
    plan: SterileGitPlan
    head_sha: str
    remotes: tuple[str, ...]
    tree_evidence: TreeEvidence

    def __post_init__(self) -> None:
        if self.head_sha.lower() != self.plan.base_sha.lower():
            raise WorkspaceSecurityError("private workspace HEAD must equal the pinned base SHA")
        if self.remotes:
            raise WorkspaceSecurityError("worker-private Git metadata must not retain remotes")


@dataclasses.dataclass(frozen=True, slots=True)
class _PinnedExecutableAdmission:
    executable: pathlib.Path
    sha256: str


@dataclasses.dataclass(frozen=True, slots=True)
class _PinnedRuntimeAdmission:
    argv: tuple[str, ...]
    executable_sha256: str


def _resolution_chain_key(path: pathlib.Path) -> str:
    value = os.path.abspath(os.fspath(path))
    return value.casefold() if os.name == "nt" else value


def _is_windows_reparse_point(file_stat: os.stat_result) -> bool:
    attributes = getattr(file_stat, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and attributes & reparse_flag)


def _pinned_executable_sha256(path: pathlib.Path) -> str:
    """Snapshot executable bytes for launch-continuity verification."""

    try:
        with path.open("rb") as source:
            return hashlib.file_digest(source, "sha256").hexdigest()
    except (OSError, ValueError) as exc:
        raise ProcessExecutionError(
            "unable to verify pinned runtime executable bytes"
        ) from exc


def _pinned_executable_descriptor_sha256(descriptor: int) -> str:
    """Hash the already-open executable object rather than reopening its pathname."""

    try:
        with os.fdopen(os.dup(descriptor), "rb") as source:
            return hashlib.file_digest(source, "sha256").hexdigest()
    except (OSError, ValueError) as exc:
        raise ProcessExecutionError(
            "unable to verify pinned runtime executable bytes"
        ) from exc


def _write_descriptor_bytes(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise ProcessExecutionError("unable to write runtime executable snapshot")
        view = view[written:]


def _open_posix_executable_snapshot() -> tuple[int, bool]:
    memfd_create = getattr(os, "memfd_create", None)
    sealing_flag = getattr(os, "MFD_ALLOW_SEALING", 0)
    if memfd_create is not None and sealing_flag:
        flags = getattr(os, "MFD_CLOEXEC", 0) | sealing_flag
        try:
            return memfd_create("nika-runtime-executable", flags), True
        except OSError as exc:
            raise ProcessExecutionError(
                "unable to create sealed runtime executable snapshot"
            ) from exc

    try:
        with tempfile.TemporaryFile(prefix="nika-runtime-executable-") as temporary:
            return os.dup(temporary.fileno()), False
    except OSError as exc:
        raise ProcessExecutionError(
            "unable to create private runtime executable snapshot"
        ) from exc


def _reopen_posix_snapshot_read_only(descriptor: int) -> int:
    source_stat = os.fstat(descriptor)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    for descriptor_root in (
        pathlib.Path("/proc/self/fd"),
        pathlib.Path("/dev/fd"),
    ):
        descriptor_path = descriptor_root / str(descriptor)
        try:
            reopened = os.open(descriptor_path, flags)
        except OSError:
            continue
        try:
            reopened_stat = os.fstat(reopened)
            if (
                reopened_stat.st_dev == source_stat.st_dev
                and reopened_stat.st_ino == source_stat.st_ino
            ):
                return reopened
        except OSError:
            pass
        try:
            os.close(reopened)
        except OSError:
            pass
    raise ProcessExecutionError(
        "read-only runtime executable snapshot descriptor is unavailable"
    )


def _seal_posix_executable_snapshot(descriptor: int) -> None:
    try:
        import fcntl
    except ImportError as exc:
        raise ProcessExecutionError(
            "runtime executable snapshot sealing is unavailable"
        ) from exc

    required_names = (
        "F_ADD_SEALS",
        "F_GET_SEALS",
        "F_SEAL_WRITE",
        "F_SEAL_GROW",
        "F_SEAL_SHRINK",
        "F_SEAL_SEAL",
    )
    if any(not hasattr(fcntl, name) for name in required_names):
        raise ProcessExecutionError(
            "runtime executable snapshot sealing is unavailable"
        )

    seals = (
        fcntl.F_SEAL_WRITE
        | fcntl.F_SEAL_GROW
        | fcntl.F_SEAL_SHRINK
        | fcntl.F_SEAL_SEAL
    )
    try:
        fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, seals)
        applied = fcntl.fcntl(descriptor, fcntl.F_GET_SEALS)
    except OSError as exc:
        raise ProcessExecutionError(
            "unable to seal runtime executable snapshot"
        ) from exc
    if applied & seals != seals:
        raise ProcessExecutionError(
            "runtime executable snapshot sealing is incomplete"
        )


def _snapshot_posix_executable(
    source_descriptor: int,
    *,
    byte_count: int,
    expected_sha256: str,
) -> int:
    snapshot_descriptor, requires_sealing = _open_posix_executable_snapshot()
    try:
        digest = hashlib.sha256()
        offset = 0
        remaining = byte_count
        while remaining > 0:
            try:
                chunk = os.pread(
                    source_descriptor,
                    min(1024 * 1024, remaining),
                    offset,
                )
            except (AttributeError, OSError) as exc:
                raise ProcessExecutionError(
                    "unable to read pinned runtime executable for snapshot"
                ) from exc
            if not chunk:
                break
            digest.update(chunk)
            _write_descriptor_bytes(snapshot_descriptor, chunk)
            offset += len(chunk)
            remaining -= len(chunk)

        if remaining != 0 or digest.hexdigest() != expected_sha256:
            raise ProcessExecutionError(
                "pinned runtime executable bytes changed before process launch"
            )

        os.fchmod(snapshot_descriptor, 0o500)
        if requires_sealing:
            _seal_posix_executable_snapshot(snapshot_descriptor)
        else:
            read_descriptor = _reopen_posix_snapshot_read_only(
                snapshot_descriptor
            )
            try:
                os.close(snapshot_descriptor)
            except OSError:
                try:
                    os.close(read_descriptor)
                except OSError:
                    pass
                raise
            snapshot_descriptor = read_descriptor

        os.lseek(snapshot_descriptor, 0, os.SEEK_SET)
        if (
            _pinned_executable_descriptor_sha256(snapshot_descriptor)
            != expected_sha256
        ):
            raise ProcessExecutionError(
                "runtime executable snapshot changed before process launch"
            )
        os.lseek(snapshot_descriptor, 0, os.SEEK_SET)
        return snapshot_descriptor
    except Exception:
        try:
            os.close(snapshot_descriptor)
        except OSError:
            pass
        raise


def _open_windows_executable_launch_lock(path: pathlib.Path) -> int:
    """Hold one final executable path against write/delete replacement."""

    try:
        import ctypes

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
            str(path),
            _WINDOWS_GENERIC_READ,
            _WINDOWS_FILE_SHARE_READ,
            None,
            _WINDOWS_OPEN_EXISTING,
            _WINDOWS_FILE_ATTRIBUTE_NORMAL | _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle is None or handle == invalid_handle:
            error_code = ctypes.get_last_error()
            raise OSError(error_code, "CreateFileW failed")
        return int(handle)
    except (AttributeError, ImportError, OSError, TypeError, ValueError) as exc:
        raise ProcessExecutionError(
            "unable to lock pinned runtime executable for launch"
        ) from exc


def _open_windows_directory_launch_lock(path: pathlib.Path) -> int:
    """Hold one process-context directory against rename/delete replacement."""

    try:
        import ctypes

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
            str(path),
            0,
            _WINDOWS_FILE_SHARE_READ | _WINDOWS_FILE_SHARE_WRITE,
            None,
            _WINDOWS_OPEN_EXISTING,
            _WINDOWS_FILE_FLAG_BACKUP_SEMANTICS | _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle is None or handle == invalid_handle:
            error_code = ctypes.get_last_error()
            raise OSError(error_code, "CreateFileW failed")
        return int(handle)
    except (AttributeError, ImportError, OSError, TypeError, ValueError) as exc:
        raise ProcessExecutionError(
            "unable to lock process directory authority for launch"
        ) from exc


def _close_windows_executable_launch_lock(handle: int) -> None:
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int
        close_handle(ctypes.c_void_p(handle))
    except (AttributeError, ImportError, OSError, TypeError, ValueError):
        return


class _PinnedExecutableLaunchGuard:
    """Revalidate the final path at the process-launch boundary.

    Windows additionally holds the pathname object with FILE_SHARE_READ only, denying
    concurrent write/delete replacement until CreateProcess has opened the image.
    """

    def __init__(
        self,
        executable: pathlib.Path,
        arguments: tuple[str, ...],
        *,
        expected_sha256: str | None = None,
    ) -> None:
        self._arguments = arguments
        if expected_sha256 is None:
            admission = _admit_pinned_executable(executable, arguments)
            self._executable = admission.executable
            self._expected_sha256 = admission.sha256
        else:
            self._executable = executable
            self._expected_sha256 = expected_sha256
        self._handle: int | None = None
        self._descriptor: int | None = None

    def __enter__(self) -> pathlib.Path:
        if os.name != "nt":
            flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                descriptor = os.open(self._executable, flags)
            except OSError as exc:
                raise ProcessExecutionError(
                    "unable to hold pinned runtime executable for launch"
                ) from exc
            self._descriptor = descriptor
            try:
                descriptor_stat = os.fstat(descriptor)
                readmitted = _resolve_pinned_executable(
                    self._executable,
                    self._arguments,
                )
                if (
                    _resolution_chain_key(readmitted)
                    != _resolution_chain_key(self._executable)
                ):
                    raise ProcessExecutionError(
                        "pinned runtime executable changed before process launch"
                    )
                try:
                    path_stat = readmitted.stat()
                except OSError as exc:
                    raise ProcessExecutionError(
                        "pinned runtime executable changed before process launch"
                    ) from exc
                if (
                    not stat.S_ISREG(descriptor_stat.st_mode)
                    or not stat.S_ISREG(path_stat.st_mode)
                    or descriptor_stat.st_dev != path_stat.st_dev
                    or descriptor_stat.st_ino != path_stat.st_ino
                ):
                    raise ProcessExecutionError(
                        "pinned runtime executable changed before process launch"
                    )
                snapshot_descriptor = _snapshot_posix_executable(
                    descriptor,
                    byte_count=descriptor_stat.st_size,
                    expected_sha256=self._expected_sha256,
                )
                self._descriptor = snapshot_descriptor
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                snapshot_stat = os.fstat(snapshot_descriptor)

                for descriptor_root in (
                    pathlib.Path("/proc/self/fd"),
                    pathlib.Path("/dev/fd"),
                ):
                    launch_path = descriptor_root / str(snapshot_descriptor)
                    try:
                        launch_stat = launch_path.stat()
                    except OSError:
                        continue
                    if (
                        launch_stat.st_dev == snapshot_stat.st_dev
                        and launch_stat.st_ino == snapshot_stat.st_ino
                    ):
                        return launch_path
                raise ProcessExecutionError(
                    "descriptor-backed executable launch is unavailable"
                )
            except Exception:
                self._close()
                raise

        self._handle = _open_windows_executable_launch_lock(self._executable)
        try:
            resolved = _resolve_pinned_executable(
                self._executable,
                self._arguments,
            )
            if _resolution_chain_key(resolved) != _resolution_chain_key(self._executable):
                raise ProcessExecutionError(
                    "pinned runtime executable changed before process launch"
                )
            if _pinned_executable_sha256(resolved) != self._expected_sha256:
                raise ProcessExecutionError(
                    "pinned runtime executable bytes changed before process launch"
                )
            return resolved
        except Exception:
            self._close()
            raise

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        self._close()

    @property
    def pass_fds(self) -> tuple[int, ...]:
        descriptor = self._descriptor
        return () if descriptor is None else (descriptor,)

    def _close(self) -> None:
        descriptor = self._descriptor
        if descriptor is not None:
            self._descriptor = None
            try:
                os.close(descriptor)
            except OSError:
                pass

        handle = self._handle
        if handle is None:
            return
        self._handle = None
        _close_windows_executable_launch_lock(handle)


class _PinnedDirectoryLaunchGuard:
    """Revalidate and, on Windows, hold workspace/cwd directory authority."""

    def __init__(
        self,
        workspace_root: pathlib.Path,
        cwd: pathlib.Path,
    ) -> None:
        self._workspace_root = workspace_root
        self._cwd = cwd
        try:
            relative = cwd.relative_to(workspace_root)
        except ValueError as exc:
            raise ProcessExecutionError(
                "process cwd escapes declared workspace root"
            ) from exc
        paths = [workspace_root]
        current = workspace_root
        for component in relative.parts:
            current = current / component
            paths.append(current)
        self._paths = tuple(paths)
        self._handles: list[int] = []

    def __enter__(self) -> pathlib.Path:
        if os.name == "nt":
            try:
                for path in self._paths:
                    self._handles.append(
                        _open_windows_directory_launch_lock(path)
                    )
            except Exception:
                self._close()
                raise
        try:
            workspace_root = ensure_real_directory_root(
                self._workspace_root,
                label="process workspace root",
            )
            cwd = ensure_real_directory_root(
                self._cwd,
                label="process cwd",
            )
            if (
                _resolution_chain_key(workspace_root)
                != _resolution_chain_key(self._workspace_root)
                or _resolution_chain_key(cwd) != _resolution_chain_key(self._cwd)
            ):
                raise ProcessExecutionError(
                    "process directory authority changed before launch"
                )
            try:
                cwd.relative_to(workspace_root)
            except ValueError as exc:
                raise ProcessExecutionError(
                    "process cwd escaped the workspace before launch"
                ) from exc
            return cwd
        except Exception:
            self._close()
            raise

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        self._close()

    def _close(self) -> None:
        while self._handles:
            _close_windows_executable_launch_lock(self._handles.pop())


def _resolve_pinned_executable(
    executable: pathlib.Path,
    arguments: tuple[str, ...],
) -> pathlib.Path:
    """Resolve an allowlisted executable without losing shell-policy evidence.

    Every named symlink hop is policy-checked before dereferencing it. The caller then
    launches only the final canonical path, so changing an allowlisted alias after
    resolution cannot redirect the Popen call through that alias.
    """

    current = executable
    seen: set[str] = set()
    for _ in range(64):
        key = _resolution_chain_key(current)
        if key in seen:
            raise ProcessExecutionError("pinned runtime executable symlink chain contains a loop")
        seen.add(key)

        validate_typed_argv((str(current), *arguments), (str(current),))
        try:
            current_stat = current.lstat()
        except OSError as exc:
            raise ProcessExecutionError("pinned runtime executable does not exist") from exc
        is_symlink = current.is_symlink()
        if _is_windows_reparse_point(current_stat) and not is_symlink:
            raise ProcessExecutionError(
                "pinned runtime executable opaque reparse indirection is forbidden"
            )
        if not is_symlink:
            break
        try:
            target = pathlib.Path(os.readlink(current))
        except OSError as exc:
            raise ProcessExecutionError("unable to inspect pinned executable symlink") from exc
        current = target if target.is_absolute() else current.parent / target
    else:
        raise ProcessExecutionError("pinned runtime executable symlink chain is too deep")

    try:
        resolved = current.resolve(strict=True)
    except OSError as exc:
        raise ProcessExecutionError("pinned runtime executable does not exist") from exc
    validate_typed_argv((str(resolved), *arguments), (str(resolved),))
    if not resolved.is_file():
        raise ProcessExecutionError("pinned runtime executable must be a regular file")
    return resolved


def _admit_pinned_executable(
    executable: pathlib.Path,
    arguments: tuple[str, ...],
) -> _PinnedExecutableAdmission:
    """Bind executable pathname policy and byte identity in one admission."""

    resolved = _resolve_pinned_executable(executable, arguments)

    if os.name == "nt":
        handle = _open_windows_executable_launch_lock(resolved)
        try:
            readmitted = _resolve_pinned_executable(resolved, arguments)
            if _resolution_chain_key(readmitted) != _resolution_chain_key(resolved):
                raise ProcessExecutionError(
                    "pinned runtime executable changed before process launch"
                )
            digest = _pinned_executable_sha256(readmitted)
            return _PinnedExecutableAdmission(readmitted, digest)
        finally:
            _close_windows_executable_launch_lock(handle)

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(resolved, flags)
    except OSError as exc:
        raise ProcessExecutionError(
            "unable to open pinned runtime executable for byte admission"
        ) from exc
    try:
        descriptor_stat = os.fstat(descriptor)
        readmitted = _resolve_pinned_executable(resolved, arguments)
        if _resolution_chain_key(readmitted) != _resolution_chain_key(resolved):
            raise ProcessExecutionError(
                "pinned runtime executable changed before process launch"
            )
        try:
            path_stat = readmitted.stat()
        except OSError as exc:
            raise ProcessExecutionError(
                "pinned runtime executable changed before process launch"
            ) from exc
        if (
            not stat.S_ISREG(descriptor_stat.st_mode)
            or not stat.S_ISREG(path_stat.st_mode)
            or descriptor_stat.st_dev != path_stat.st_dev
            or descriptor_stat.st_ino != path_stat.st_ino
        ):
            raise ProcessExecutionError(
                "pinned runtime executable changed before process launch"
            )
        return _PinnedExecutableAdmission(
            readmitted,
            _pinned_executable_descriptor_sha256(descriptor),
        )
    finally:
        os.close(descriptor)


def _pinned_runtime_argv(
    argv: collections.abc.Sequence[str],
    allowed_executables: collections.abc.Iterable[str],
) -> _PinnedRuntimeAdmission:
    allowed = tuple(allowed_executables)
    typed = validate_typed_argv(argv, allowed)
    executable = pathlib.Path(typed[0])
    if not executable.is_absolute():
        raise ProcessExecutionError(
            "runtime executable must be an absolute pinned path; PATH/CWD search is forbidden"
        )
    admission = _admit_pinned_executable(executable, typed[1:])
    admitted_argv = (str(admission.executable), *typed[1:])
    validate_typed_argv(admitted_argv, allowed)
    return _PinnedRuntimeAdmission(admitted_argv, admission.sha256)


def _validate_process_workspace_root(root: pathlib.Path) -> pathlib.Path:
    probe_policy = WorkspacePathPolicy(("_nika_process_tmp",))
    ensure_path_policy(root, "_nika_process_tmp", probe_policy)
    return ensure_real_directory_root(root, label="process workspace root")


def _prepare_process_environment(
    *,
    source: collections.abc.Mapping[str, str],
    workspace_root: pathlib.Path,
) -> dict[str, str]:
    temp_policy = WorkspacePathPolicy(("_nika_process_tmp",))
    temp_root = ensure_path_policy(workspace_root, "_nika_process_tmp", temp_policy)
    temp_root.mkdir(parents=False, exist_ok=True)
    temp_root = ensure_path_policy(
        workspace_root,
        "_nika_process_tmp",
        temp_policy,
        must_exist=True,
    )
    if not temp_root.is_dir():
        raise ProcessExecutionError("worker temp path must be a directory")
    return sterile_process_environment(source, temp_root=temp_root)


def _process_isolation_class() -> toolsmith_contracts.IsolationClass:
    return (
        toolsmith_contracts.IsolationClass.PROCESS_CONTAINED
        if os.name == "nt"
        else toolsmith_contracts.IsolationClass.POLICY_ONLY
    )


def _cancelled_before_launch(typed_argv: tuple[str, ...]) -> ProcessExecutionResult:
    return ProcessExecutionResult(
        argv=typed_argv,
        returncode=1,
        stdout="",
        stderr="",
        timed_out=False,
        cancelled=True,
        output_limit_exceeded=False,
        isolation_class=_process_isolation_class(),
    )


def _timed_out_before_launch(typed_argv: tuple[str, ...]) -> ProcessExecutionResult:
    return ProcessExecutionResult(
        argv=typed_argv,
        returncode=1,
        stdout="",
        stderr="",
        timed_out=True,
        cancelled=False,
        output_limit_exceeded=False,
        isolation_class=_process_isolation_class(),
    )


def run_typed_process(
    argv: collections.abc.Sequence[str],
    *,
    process_policy: toolsmith_contracts.ProcessPolicy,
    resource_budget: toolsmith_contracts.ResourceBudget,
    cwd: pathlib.Path,
    environment: collections.abc.Mapping[str, str],
    cancellation_event: threading.Event | None = None,
    workspace_root: pathlib.Path | None = None,
) -> ProcessExecutionResult:
    if type(resource_budget) is not toolsmith_contracts.ResourceBudget:
        raise ProcessExecutionError("resource budget carrier is invalid")
    try:
        resource_budget.__post_init__()
        deadline = time.monotonic() + resource_budget.timeout_seconds
    except (OverflowError, TypeError, ValueError) as exc:
        raise ProcessExecutionError("resource budget is invalid") from exc
    limit = resource_budget.max_output_bytes

    runtime_admission = _pinned_runtime_argv(argv, process_policy.allowed_executables)
    typed_argv = runtime_admission.argv
    launch_guard = _PinnedExecutableLaunchGuard(
        pathlib.Path(typed_argv[0]),
        typed_argv[1:],
        expected_sha256=runtime_admission.executable_sha256,
    )
    raw_cwd = pathlib.Path(cwd)
    raw_workspace_root = raw_cwd if workspace_root is None else pathlib.Path(workspace_root)
    workspace_root = _validate_process_workspace_root(raw_workspace_root)
    cwd = raw_cwd.resolve(strict=True)
    if not cwd.is_dir():
        raise ProcessExecutionError("process cwd must be a directory")
    try:
        cwd.relative_to(workspace_root)
    except ValueError as exc:
        raise ProcessExecutionError("process cwd escapes declared workspace root") from exc
    if cancellation_event is not None and cancellation_event.is_set():
        return _cancelled_before_launch(typed_argv)
    process_environment = _prepare_process_environment(
        source=environment,
        workspace_root=workspace_root,
    )
    if cancellation_event is not None and cancellation_event.is_set():
        return _cancelled_before_launch(typed_argv)

    output = {"stdout": bytearray(), "stderr": bytearray()}
    total_output = 0
    overflow = threading.Event()
    output_lock = threading.Lock()

    creationflags, start_new_session = process_group_popen_options()

    with launch_guard as launch_executable:
        with _PinnedDirectoryLaunchGuard(workspace_root, cwd) as launch_cwd:
            if cancellation_event is not None and cancellation_event.is_set():
                return _cancelled_before_launch(typed_argv)
            if time.monotonic() >= deadline:
                return _timed_out_before_launch(typed_argv)
            launch_argv = (str(launch_executable), *typed_argv[1:])
            if os.name == "nt":
                process = subprocess.Popen(
                    launch_argv,
                    cwd=launch_cwd,
                    env=process_environment,
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=creationflags,
                    start_new_session=start_new_session,
                )
            else:
                process = subprocess.Popen(
                    typed_argv,
                    executable=str(launch_executable),
                    pass_fds=launch_guard.pass_fds,
                    cwd=launch_cwd,
                    env=process_environment,
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=creationflags,
                    start_new_session=start_new_session,
                )

    with _WindowsJob() as job:
        if os.name == "nt":
            try:
                job.assign(int(process._handle))  # type: ignore[attr-defined]
            except ProcessContainmentError as exc:
                _terminate_process_tree(process, job)
                try:
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                raise ProcessExecutionError(
                    "failed to establish Windows process-tree containment"
                ) from exc

        def drain(stream_name: str, stream: collections.abc.Iterator[bytes]) -> None:
            nonlocal total_output
            for chunk in stream:
                with output_lock:
                    remaining = limit - total_output
                    if remaining <= 0:
                        overflow.set()
                        return
                    accepted = chunk[:remaining]
                    output[stream_name].extend(accepted)
                    total_output += len(accepted)
                    if len(chunk) > remaining:
                        overflow.set()
                        return

        assert process.stdout is not None
        assert process.stderr is not None
        stdout_thread = threading.Thread(
            target=drain,
            args=("stdout", iter(lambda: process.stdout.read(65536), b"")),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=drain,
            args=("stderr", iter(lambda: process.stderr.read(65536), b"")),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        timed_out = False
        cancelled = False
        while process.poll() is None:
            if overflow.is_set():
                _terminate_process_tree(process, job)
                break
            if cancellation_event is not None and cancellation_event.is_set():
                cancelled = True
                _terminate_process_tree(process, job)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _terminate_process_tree(process, job)
                break
            time.sleep(0.02)

        try:
            returncode = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _terminate_process_tree(process, job)
            returncode = process.wait(timeout=5)
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)

    forced_termination = timed_out or cancelled or overflow.is_set()
    if forced_termination and returncode == 0:
        returncode = 1

    return ProcessExecutionResult(
        argv=typed_argv,
        returncode=returncode,
        stdout=bytes(output["stdout"]).decode("utf-8", errors="replace"),
        stderr=bytes(output["stderr"]).decode("utf-8", errors="replace"),
        timed_out=timed_out,
        cancelled=cancelled,
        output_limit_exceeded=overflow.is_set(),
        isolation_class=_process_isolation_class(),
    )


def _validate_branch_name(branch_name: str) -> None:
    if (
        not branch_name
        or branch_name != branch_name.strip()
        or branch_name.startswith("-")
        or "\x00" in branch_name
        or any(ord(character) < 32 or ord(character) == 127 for character in branch_name)
    ):
        raise WorkspaceSecurityError("branch name is empty, ambiguous or contains control data")


def _git(
    argv: collections.abc.Sequence[str],
    *,
    cwd: pathlib.Path,
    environment: collections.abc.Mapping[str, str],
    timeout_seconds: int = 60,
    expected_executable_sha256: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = tuple(argv)
    if not command:
        raise WorkspaceSecurityError("git command is empty")
    try:
        executable = pathlib.Path(command[0])
        if executable.is_absolute():
            launch_guard = _PinnedExecutableLaunchGuard(
                executable,
                command[1:],
                expected_sha256=expected_executable_sha256,
            )
            with launch_guard as launch_executable:
                launch_command = (
                    (str(launch_executable), *command[1:])
                    if os.name == "nt"
                    else command
                )
                launch_options = (
                    {}
                    if os.name == "nt"
                    else {
                        "executable": str(launch_executable),
                        "pass_fds": launch_guard.pass_fds,
                    }
                )
                result = subprocess.run(
                    launch_command,
                    cwd=cwd,
                    env=dict(environment),
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout_seconds,
                    check=False,
                    **launch_options,
                )
        else:
            result = subprocess.run(
                command,
                cwd=cwd,
                env=dict(environment),
                shell=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
            )
    except ProcessExecutionError:
        raise WorkspaceSecurityError(
            "git command executable authority changed before launch"
        ) from None
    except subprocess.TimeoutExpired:
        raise WorkspaceSecurityError("git command timed out") from None
    except (OSError, subprocess.SubprocessError):
        raise WorkspaceSecurityError("git command could not be executed") from None
    if result.returncode != 0:
        raise WorkspaceSecurityError(f"git command failed (exit {result.returncode})")
    return result


def _resolve_host_git_executable(git_executable: str) -> _PinnedExecutableAdmission:
    requested = git_executable.strip()
    if not requested or requested != git_executable or "\x00" in requested:
        raise WorkspaceSecurityError("git executable identity is empty or ambiguous")

    candidate = pathlib.Path(requested)
    if not candidate.is_absolute():
        if pathlib.PureWindowsPath(requested).name != requested:
            raise WorkspaceSecurityError(
                "relative path-qualified Git executable is forbidden; use a host PATH name or absolute path"
            )
        host_path = os.environ.get("PATH", "")
        discovered = shutil.which(requested, path=host_path)
        if discovered is None:
            raise WorkspaceSecurityError("trusted host Git executable was not found")
        candidate = pathlib.Path(discovered)

    try:
        return _admit_pinned_executable(candidate, ())
    except ProcessExecutionError as exc:
        raise WorkspaceSecurityError("trusted host Git executable is invalid") from exc


def _private_git_job_root(plan: SterileGitPlan) -> pathlib.Path:
    raw_job_root = plan.private_git_dir.parent
    if plan.worktree_root.parent != raw_job_root:
        raise WorkspaceSecurityError("private Git paths do not share the trusted job root")
    if plan.private_git_dir.name != "_nika_private_git" or plan.worktree_root.name != "worktree":
        raise WorkspaceSecurityError("private Git paths do not match the canonical workspace plan")

    job_root = ensure_real_directory_root(raw_job_root, label="job workspace root")
    repository_root = plan.repository_root.resolve(strict=False)
    if (
        repository_root == job_root
        or repository_root in job_root.parents
        or job_root in repository_root.parents
    ):
        raise WorkspaceSecurityError("job workspace and production repository must be fully disjoint")
    return job_root


def prepare_private_git_workspace(
    plan: SterileGitPlan,
    *,
    git_executable: str = "git",
) -> PreparedGitWorkspace:
    _validate_branch_name(plan.branch_name)
    job_root = _private_git_job_root(plan)
    git_admission = _resolve_host_git_executable(git_executable)
    git_executable = str(git_admission.executable)
    git_executable_sha256 = git_admission.sha256
    if plan.private_git_dir.exists() or plan.worktree_root.exists():
        raise WorkspaceSecurityError("job-private Git paths already exist; refusing ambiguous reuse")
    if not (plan.repository_root / ".git").exists():
        raise WorkspaceSecurityError("production repository must expose trusted Git metadata")

    _git(
        (git_executable, "check-ref-format", "--branch", plan.branch_name),
        cwd=job_root,
        environment=plan.environment,
        expected_executable_sha256=git_executable_sha256,
    )

    null_hooks = "NUL" if os.name == "nt" else "/dev/null"
    clone_argv = (
        git_executable,
        "-c",
        "credential.helper=",
        "-c",
        f"core.hooksPath={null_hooks}",
        "-c",
        "protocol.file.allow=always",
        "-c",
        "protocol.ext.allow=never",
        "clone",
        "--bare",
        "--no-hardlinks",
        "--no-tags",
        str(plan.repository_root),
        str(plan.private_git_dir),
    )
    _git(
        clone_argv,
        cwd=job_root,
        environment=plan.environment,
        expected_executable_sha256=git_executable_sha256,
    )

    git_prefix = (git_executable, *plan.config_args, "--git-dir", str(plan.private_git_dir))
    remote_names = tuple(
        item.strip()
        for item in _git(
            (*git_prefix, "remote"),
            cwd=job_root,
            environment=plan.environment,
            expected_executable_sha256=git_executable_sha256,
        ).stdout.splitlines()
        if item.strip()
    )
    for remote_name in remote_names:
        _git(
            (*git_prefix, "remote", "remove", remote_name),
            cwd=job_root,
            environment=plan.environment,
            expected_executable_sha256=git_executable_sha256,
        )
    remaining_remotes = tuple(
        item.strip()
        for item in _git(
            (*git_prefix, "remote"),
            cwd=job_root,
            environment=plan.environment,
            expected_executable_sha256=git_executable_sha256,
        ).stdout.splitlines()
        if item.strip()
    )
    if remaining_remotes:
        raise WorkspaceSecurityError("failed to remove all worker-private Git remotes")

    base_result = _git(
        (*git_prefix, "rev-parse", "--verify", f"{plan.base_sha}^{{commit}}"),
        cwd=job_root,
        environment=plan.environment,
        expected_executable_sha256=git_executable_sha256,
    )
    if base_result.stdout.strip().lower() != plan.base_sha.lower():
        raise WorkspaceSecurityError("pinned base SHA is not the exact private Git commit")

    collision_argv = (
        *git_prefix,
        "show-ref",
        "--verify",
        "--quiet",
        f"refs/heads/{plan.branch_name}",
    )
    try:
        collision_guard = _PinnedExecutableLaunchGuard(
            pathlib.Path(collision_argv[0]),
            collision_argv[1:],
            expected_sha256=git_executable_sha256,
        )
        with collision_guard as launch_executable:
            collision_command = (
                (str(launch_executable), *collision_argv[1:])
                if os.name == "nt"
                else collision_argv
            )
            collision_options = (
                {}
                if os.name == "nt"
                else {
                    "executable": str(launch_executable),
                    "pass_fds": collision_guard.pass_fds,
                }
            )
            collision = subprocess.run(
                collision_command,
                cwd=job_root,
                env=dict(plan.environment),
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                **collision_options,
            )
    except ProcessExecutionError:
        raise WorkspaceSecurityError(
            "unable to prove job branch collision executable authority"
        ) from None
    if collision.returncode == 0:
        raise WorkspaceSecurityError("job branch already exists in private metadata")
    if collision.returncode not in {1}:
        raise WorkspaceSecurityError("unable to prove job branch collision state")

    ensure_real_directory_root(plan.private_git_dir.parent, label="job workspace root")
    plan.worktree_root.mkdir(parents=False, exist_ok=False)
    _git(
        (
            *git_prefix,
            "--work-tree",
            str(plan.worktree_root),
            "checkout",
            "-f",
            "-b",
            plan.branch_name,
            plan.base_sha,
        ),
        cwd=job_root,
        environment=plan.environment,
        expected_executable_sha256=git_executable_sha256,
    )
    if (plan.worktree_root / ".git").exists():
        raise WorkspaceSecurityError("worker-visible worktree unexpectedly contains .git metadata")

    head = _git(
        (*git_prefix, "rev-parse", "HEAD"),
        cwd=job_root,
        environment=plan.environment,
        expected_executable_sha256=git_executable_sha256,
    ).stdout.strip()
    tree_evidence = collect_tree_evidence(plan.worktree_root)
    return PreparedGitWorkspace(plan, head, remaining_remotes, tree_evidence)


def cleanup_private_git_workspace(plan: SterileGitPlan) -> None:
    raw_job_root = plan.private_git_dir.parent
    job_root = _private_git_job_root(plan)

    for root in (plan.worktree_root, plan.private_git_dir):
        assert_cleanup_tree_safe(root)
    ensure_real_directory_root(raw_job_root, label="job workspace root")
    if raw_job_root.resolve(strict=True) != job_root:
        raise WorkspaceSecurityError("job workspace root identity changed before cleanup")
    for root in (plan.worktree_root, plan.private_git_dir):
        if root.exists():
            ensure_real_directory_root(raw_job_root, label="job workspace root")
            if raw_job_root.resolve(strict=True) != job_root:
                raise WorkspaceSecurityError("job workspace root identity changed during cleanup")
            try:
                shutil.rmtree(root)
            except OSError as exc:
                raise WorkspaceSecurityError(f"unable to clean private workspace: {root.name}") from exc
