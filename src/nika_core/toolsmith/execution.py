from __future__ import annotations

import collections.abc
import ctypes
import dataclasses
import os
import pathlib
import signal
import subprocess
import threading
import time

from nika_core.toolsmith import contracts as toolsmith_contracts
from nika_core.toolsmith.workspace_security import (
    SterileGitPlan,
    TreeEvidence,
    WorkspaceSecurityError,
    collect_tree_evidence,
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

    def __enter__(self) -> _WindowsJob:
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


def run_typed_process(
    argv: collections.abc.Sequence[str],
    *,
    process_policy: toolsmith_contracts.ProcessPolicy,
    resource_budget: toolsmith_contracts.ResourceBudget,
    cwd: pathlib.Path,
    environment: collections.abc.Mapping[str, str],
    cancellation_event: threading.Event | None = None,
) -> ProcessExecutionResult:
    typed_argv = validate_typed_argv(argv, process_policy.allowed_executables)
    cwd = cwd.resolve(strict=True)
    if not cwd.is_dir():
        raise ProcessExecutionError("process cwd must be a directory")

    limit = resource_budget.max_output_bytes
    output = {"stdout": bytearray(), "stderr": bytearray()}
    overflow = threading.Event()
    output_lock = threading.Lock()

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    process = subprocess.Popen(
        typed_argv,
        cwd=cwd,
        env=dict(environment),
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
            for chunk in stream:
                with output_lock:
                    remaining = limit - len(output[stream_name])
                    if remaining <= 0:
                        overflow.set()
                        return
                    output[stream_name].extend(chunk[:remaining])
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

    isolation_class = (
        toolsmith_contracts.IsolationClass.PROCESS_CONTAINED
        if os.name == "nt"
        else toolsmith_contracts.IsolationClass.POLICY_ONLY
    )
    return ProcessExecutionResult(
        argv=typed_argv,
        returncode=returncode,
        stdout=bytes(output["stdout"]).decode("utf-8", errors="replace"),
        stderr=bytes(output["stderr"]).decode("utf-8", errors="replace"),
        timed_out=timed_out,
        cancelled=cancelled,
        output_limit_exceeded=overflow.is_set(),
        isolation_class=isolation_class,
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
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
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


def prepare_private_git_workspace(
    plan: SterileGitPlan,
    *,
    git_executable: str = "git",
) -> PreparedGitWorkspace:
    _validate_branch_name(plan.branch_name)
    if plan.private_git_dir.exists() or plan.worktree_root.exists():
        raise WorkspaceSecurityError("job-private Git paths already exist; refusing ambiguous reuse")
    if not (plan.repository_root / ".git").exists():
        raise WorkspaceSecurityError("production repository must expose trusted Git metadata")

    plan.private_git_dir.parent.mkdir(parents=True, exist_ok=True)
    clone_argv = (
        git_executable,
        *plan.config_args,
        "clone",
        "--bare",
        "--no-hardlinks",
        "--no-tags",
        str(plan.repository_root),
        str(plan.private_git_dir),
    )
    _git(clone_argv, cwd=plan.private_git_dir.parent, environment=plan.environment)

    git_prefix = (git_executable, *plan.config_args, "--git-dir", str(plan.private_git_dir))
    remote_names = tuple(
        item.strip()
        for item in _git(
            (*git_prefix, "remote"),
            cwd=plan.private_git_dir.parent,
            environment=plan.environment,
        ).stdout.splitlines()
        if item.strip()
    )
    for remote_name in remote_names:
        _git(
            (*git_prefix, "remote", "remove", remote_name),
            cwd=plan.private_git_dir.parent,
            environment=plan.environment,
        )
    remaining_remotes = tuple(
        item.strip()
        for item in _git(
            (*git_prefix, "remote"),
            cwd=plan.private_git_dir.parent,
            environment=plan.environment,
        ).stdout.splitlines()
        if item.strip()
    )
    if remaining_remotes:
        raise WorkspaceSecurityError("failed to remove all worker-private Git remotes")

    base_result = _git(
        (*git_prefix, "rev-parse", "--verify", f"{plan.base_sha}^{{commit}}"),
        cwd=plan.private_git_dir.parent,
        environment=plan.environment,
    )
    if base_result.stdout.strip().lower() != plan.base_sha.lower():
        raise WorkspaceSecurityError("pinned base SHA is not the exact private Git commit")

    collision = subprocess.run(
        (*git_prefix, "show-ref", "--verify", "--quiet", f"refs/heads/{plan.branch_name}"),
        cwd=plan.private_git_dir.parent,
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

    plan.worktree_root.mkdir(parents=False, exist_ok=False)
    checkout = _git(
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
        cwd=plan.private_git_dir.parent,
        environment=plan.environment,
    )
    del checkout
    if (plan.worktree_root / ".git").exists():
        raise WorkspaceSecurityError("worker-visible worktree unexpectedly contains .git metadata")

    head = _git(
        (*git_prefix, "rev-parse", "HEAD"),
        cwd=plan.private_git_dir.parent,
        environment=plan.environment,
    ).stdout.strip()
    tree_evidence = collect_tree_evidence(plan.worktree_root)
    return PreparedGitWorkspace(plan, head, remaining_remotes, tree_evidence)
