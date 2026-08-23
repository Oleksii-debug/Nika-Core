from __future__ import annotations

import collections.abc
import ctypes
import dataclasses
import os
import pathlib
import shutil
import signal
import stat
import subprocess
import threading
import time
from typing import Self

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


class _WindowsJob:
    def __init__(self) -> None:
        self._handle: int | None = None

    @property
    def active(self) -> bool:
        return self._handle is not None

    def assign(self, process_handle: int) -> None:
        if os.name != "nt":
            return

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_job = kernel32.CreateJobObjectW
        create_job.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
        create_job.restype = ctypes.c_void_p
        set_information = kernel32.SetInformationJobObject
        set_information.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
        set_information.restype = ctypes.c_int
        assign_process = kernel32.AssignProcessToJobObject
        assign_process.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        assign_process.restype = ctypes.c_int

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        job = create_job(None, None)
        if not job:
            raise ProcessExecutionError(
                f"CreateJobObjectW failed with Win32 error {ctypes.get_last_error()}"
            )
        information = ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = 0x00002000
        if not set_information(job, 9, ctypes.byref(information), ctypes.sizeof(information)):
            kernel32.CloseHandle(job)
            raise ProcessExecutionError(
                f"SetInformationJobObject failed with Win32 error {ctypes.get_last_error()}"
            )
        if not assign_process(job, ctypes.c_void_p(process_handle)):
            kernel32.CloseHandle(job)
            raise ProcessExecutionError(
                f"AssignProcessToJobObject failed with Win32 error {ctypes.get_last_error()}"
            )
        self._handle = int(job)

    def close(self) -> None:
        if self._handle is None or os.name != "nt":
            self._handle = None
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle(ctypes.c_void_p(self._handle))
        self._handle = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


def _terminate_process_tree(process: subprocess.Popen[bytes], job: _WindowsJob) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt" and job.active:
        job.close()
        return
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
    process.kill()


def _resolution_chain_key(path: pathlib.Path) -> str:
    value = os.path.abspath(os.fspath(path))
    return value.casefold() if os.name == "nt" else value


def _is_windows_reparse_point(file_stat: os.stat_result) -> bool:
    attributes = getattr(file_stat, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and attributes & reparse_flag)


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


def _pinned_runtime_argv(
    argv: collections.abc.Sequence[str],
    allowed_executables: collections.abc.Iterable[str],
) -> tuple[str, ...]:
    allowed = tuple(allowed_executables)
    typed = validate_typed_argv(argv, allowed)
    executable = pathlib.Path(typed[0])
    if not executable.is_absolute():
        raise ProcessExecutionError(
            "runtime executable must be an absolute pinned path; PATH/CWD search is forbidden"
        )
    resolved = _resolve_pinned_executable(executable, typed[1:])
    validate_typed_argv((str(resolved), *typed[1:]), allowed)
    return (str(resolved), *typed[1:])


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
    typed_argv = _pinned_runtime_argv(argv, process_policy.allowed_executables)
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

    limit = resource_budget.max_output_bytes
    output = {"stdout": bytearray(), "stderr": bytearray()}
    total_output = 0
    overflow = threading.Event()
    output_lock = threading.Lock()

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    process = subprocess.Popen(
        typed_argv,
        cwd=cwd,
        env=process_environment,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creationflags,
        start_new_session=os.name != "nt",
    )

    with _WindowsJob() as job:
        if os.name == "nt":
            job.assign(int(process._handle))  # type: ignore[attr-defined]

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

        deadline = time.monotonic() + resource_budget.timeout_seconds
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
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        tuple(argv),
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
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise WorkspaceSecurityError(message[:2000])
    return result


def _resolve_host_git_executable(git_executable: str) -> str:
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
        return str(_resolve_pinned_executable(candidate, ()))
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
    git_executable = _resolve_host_git_executable(git_executable)
    if plan.private_git_dir.exists() or plan.worktree_root.exists():
        raise WorkspaceSecurityError("job-private Git paths already exist; refusing ambiguous reuse")
    if not (plan.repository_root / ".git").exists():
        raise WorkspaceSecurityError("production repository must expose trusted Git metadata")

    _git(
        (git_executable, "check-ref-format", "--branch", plan.branch_name),
        cwd=job_root,
        environment=plan.environment,
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
    _git(clone_argv, cwd=job_root, environment=plan.environment)

    git_prefix = (git_executable, *plan.config_args, "--git-dir", str(plan.private_git_dir))
    remote_names = tuple(
        item.strip()
        for item in _git(
            (*git_prefix, "remote"),
            cwd=job_root,
            environment=plan.environment,
        ).stdout.splitlines()
        if item.strip()
    )
    for remote_name in remote_names:
        _git(
            (*git_prefix, "remote", "remove", remote_name),
            cwd=job_root,
            environment=plan.environment,
        )
    remaining_remotes = tuple(
        item.strip()
        for item in _git(
            (*git_prefix, "remote"),
            cwd=job_root,
            environment=plan.environment,
        ).stdout.splitlines()
        if item.strip()
    )
    if remaining_remotes:
        raise WorkspaceSecurityError("failed to remove all worker-private Git remotes")

    base_result = _git(
        (*git_prefix, "rev-parse", "--verify", f"{plan.base_sha}^{{commit}}"),
        cwd=job_root,
        environment=plan.environment,
    )
    if base_result.stdout.strip().lower() != plan.base_sha.lower():
        raise WorkspaceSecurityError("pinned base SHA is not the exact private Git commit")

    collision = subprocess.run(
        (*git_prefix, "show-ref", "--verify", "--quiet", f"refs/heads/{plan.branch_name}"),
        cwd=job_root,
        env=dict(plan.environment),
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
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
    )
    if (plan.worktree_root / ".git").exists():
        raise WorkspaceSecurityError("worker-visible worktree unexpectedly contains .git metadata")

    head = _git(
        (*git_prefix, "rev-parse", "HEAD"),
        cwd=job_root,
        environment=plan.environment,
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
