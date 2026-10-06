from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import threading

import pytest

import nika_core.toolsmith.execution as execution_module
from nika_core.toolsmith import IsolationClass, ProcessPolicy, ResourceBudget
from nika_core.toolsmith.execution import prepare_private_git_workspace, run_typed_process
from nika_core.toolsmith.workspace_security import (
    WorkspaceSecurityError,
    make_sterile_git_plan,
    sterile_git_environment,
)


def _git(cwd: pathlib.Path, *args: str) -> str:
    result = subprocess.run(
        ("git", *args),
        cwd=cwd,
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def _python_process_policy() -> ProcessPolicy:
    canonical = str(pathlib.Path(sys.executable).resolve(strict=True))
    allowed = (sys.executable,) if canonical == sys.executable else (sys.executable, canonical)
    return ProcessPolicy(allowed)


def _make_source_repository(tmp_path: pathlib.Path) -> tuple[pathlib.Path, str]:
    if shutil.which("git") is None:
        pytest.skip("Git CLI unavailable")
    repository = tmp_path / "production"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Nika Test")
    _git(repository, "config", "user.email", "nika@example.invalid")
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    source = repository / "src"
    source.mkdir()
    (source / "модуль.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repository, "add", "README.md", "src/модуль.py")
    _git(repository, "commit", "-m", "base")
    return repository, _git(repository, "rev-parse", "HEAD")


def test_prepare_private_git_workspace_has_no_remote_or_visible_dot_git(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha = _make_source_repository(tmp_path)
    job_root = tmp_path / "jobs" / "job-1"
    job_root.mkdir(parents=True)
    plan = make_sterile_git_plan(
        repository_root=repository,
        job_root=job_root,
        branch_name="toolsmith/job-1",
        base_sha=base_sha,
        source_environment={"PATH": os.environ.get("PATH", "")},
    )

    prepared = prepare_private_git_workspace(plan)

    assert prepared.head_sha == base_sha
    assert prepared.remotes == ()
    assert not (prepared.plan.worktree_root / ".git").exists()
    assert (prepared.plan.worktree_root / "README.md").read_text(encoding="utf-8") == "base\n"
    assert tuple(item.path for item in prepared.tree_evidence.files) == (
        "README.md",
        "src/модуль.py",
    )
    private_remotes = _git(
        prepared.plan.private_git_dir.parent,
        "--git-dir",
        str(prepared.plan.private_git_dir),
        "remote",
    )
    assert private_remotes == ""


def test_private_git_workspace_uses_pinned_executable_launch_guard(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, base_sha = _make_source_repository(tmp_path)
    job_root = tmp_path / "jobs" / "job-guard"
    job_root.mkdir(parents=True)
    plan = make_sterile_git_plan(
        repository_root=repository,
        job_root=job_root,
        branch_name="toolsmith/job-guard",
        base_sha=base_sha,
        source_environment={"PATH": os.environ.get("PATH", "")},
    )
    observed: list[pathlib.Path] = []
    observed_expected_sha256: list[str | None] = []
    original_guard = execution_module._PinnedExecutableLaunchGuard

    class RecordingGuard(original_guard):
        def __init__(
            self,
            executable: pathlib.Path,
            arguments: tuple[str, ...],
            *,
            expected_sha256: str | None = None,
        ) -> None:
            observed_expected_sha256.append(expected_sha256)
            super().__init__(
                executable,
                arguments,
                expected_sha256=expected_sha256,
            )

        def __enter__(self) -> pathlib.Path:
            resolved = super().__enter__()
            observed.append(resolved)
            return resolved

    monkeypatch.setattr(
        execution_module,
        "_PinnedExecutableLaunchGuard",
        RecordingGuard,
    )

    prepared = prepare_private_git_workspace(plan)

    assert prepared.head_sha == base_sha
    assert observed
    assert all(path.is_absolute() for path in observed)
    assert observed_expected_sha256
    assert all(value is not None for value in observed_expected_sha256)
    assert len(set(observed_expected_sha256)) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics only")
def test_windows_executable_launch_guard_denies_replace_until_release(
    tmp_path: pathlib.Path,
) -> None:
    executable = tmp_path / "runner.exe"
    replacement = tmp_path / "replacement.exe"
    executable.write_bytes(b"expected executable bytes")
    replacement.write_bytes(b"replacement executable bytes")

    with execution_module._PinnedExecutableLaunchGuard(executable, ()):
        with pytest.raises(OSError):
            os.replace(replacement, executable)
        assert executable.read_bytes() == b"expected executable bytes"
        assert replacement.read_bytes() == b"replacement executable bytes"

    os.replace(replacement, executable)
    assert executable.read_bytes() == b"replacement executable bytes"


def test_typed_runner_rejects_final_executable_identity_change(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = pathlib.Path(sys.executable).resolve(strict=True)
    replacement = canonical.with_name(canonical.name + ".replacement")
    original_resolve = execution_module._resolve_pinned_executable
    calls = 0

    def changing_resolve(
        executable: pathlib.Path,
        arguments: tuple[str, ...],
    ) -> pathlib.Path:
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_resolve(executable, arguments)
        return replacement

    monkeypatch.setattr(
        execution_module,
        "_resolve_pinned_executable",
        changing_resolve,
    )

    with pytest.raises(
        execution_module.ProcessExecutionError,
        match="changed before process launch",
    ):
        run_typed_process(
            (sys.executable, "-c", "print('must not run')"),
            process_policy=_python_process_policy(),
            resource_budget=ResourceBudget(
                timeout_seconds=5,
                max_output_bytes=4096,
                max_changed_files=1,
            ),
            cwd=tmp_path,
            environment=sterile_git_environment(
                {"PATH": os.environ.get("PATH", "")}
            ),
        )

    assert calls == 2


def test_executable_launch_guard_rejects_same_path_byte_replacement(
    tmp_path: pathlib.Path,
) -> None:
    executable = tmp_path / "runner"
    replacement = tmp_path / "replacement"
    executable.write_bytes(b"trusted executable bytes")
    replacement.write_bytes(b"replacement executable bytes")

    guard = execution_module._PinnedExecutableLaunchGuard(executable, ())
    os.replace(replacement, executable)

    with pytest.raises(
        execution_module.ProcessExecutionError,
        match="bytes changed before process launch",
    ):
        with guard:
            raise AssertionError("changed executable must not cross launch guard")


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor launch only")
def test_posix_launch_guard_keeps_exact_descriptor_through_exec(
    tmp_path: pathlib.Path,
) -> None:
    executable = tmp_path / "runner"
    replacement = tmp_path / "replacement"
    executable.write_text(
        "#!/bin/sh\nprintf 'descriptor-bound\\n'\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    replacement.write_text(
        "#!/bin/sh\nprintf 'replacement\\n'\n",
        encoding="utf-8",
    )
    replacement.chmod(0o700)

    guard = execution_module._PinnedExecutableLaunchGuard(executable, ())
    with guard as launch_executable:
        assert guard.pass_fds
        os.replace(replacement, executable)
        result = subprocess.run(
            (str(executable),),
            executable=str(launch_executable),
            pass_fds=guard.pass_fds,
            cwd=tmp_path,
            env=sterile_git_environment({"PATH": os.environ.get("PATH", "")}),
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    assert result.returncode == 0
    assert result.stdout.strip() == "descriptor-bound"
    assert executable.read_text(encoding="utf-8") == "#!/bin/sh\nprintf 'replacement\\n'\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor launch only")
def test_posix_typed_runner_survives_path_swap_at_popen(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "runner"
    replacement = tmp_path / "replacement"
    executable.write_text(
        "#!/bin/sh\nprintf 'trusted-executable\\n'\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    replacement.write_text(
        "#!/bin/sh\nprintf 'replacement\\n'\n",
        encoding="utf-8",
    )
    replacement.chmod(0o700)
    original_popen = execution_module.subprocess.Popen
    swapped = False

    def swapping_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        nonlocal swapped
        if not swapped:
            os.replace(replacement, executable)
            swapped = True
        return original_popen(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(execution_module.subprocess, "Popen", swapping_popen)

    result = run_typed_process(
        (str(executable),),
        process_policy=ProcessPolicy((str(executable),)),
        resource_budget=ResourceBudget(
            timeout_seconds=5,
            max_output_bytes=4096,
            max_changed_files=1,
        ),
        cwd=tmp_path,
        environment=sterile_git_environment(
            {"PATH": os.environ.get("PATH", "")}
        ),
    )

    assert swapped is True
    assert result.returncode == 0
    assert result.stdout.strip() == "trusted-executable"
    assert executable.read_text(encoding="utf-8") == "#!/bin/sh\nprintf 'replacement\\n'\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable snapshot only")
def test_posix_launch_guard_survives_same_inode_byte_mutation(
    tmp_path: pathlib.Path,
) -> None:
    executable = tmp_path / "runner"
    trusted = "#!/bin/sh\nprintf 'snapshot-trusted\\n'\n"
    replacement = "#!/bin/sh\nprintf 'same-inode-replacement\\n'\n"
    executable.write_text(trusted, encoding="utf-8")
    executable.chmod(0o700)

    guard = execution_module._PinnedExecutableLaunchGuard(executable, ())
    with guard as launch_executable:
        original_stat = executable.stat()
        executable.write_text(replacement, encoding="utf-8")
        mutated_stat = executable.stat()
        assert (mutated_stat.st_dev, mutated_stat.st_ino) == (
            original_stat.st_dev,
            original_stat.st_ino,
        )
        result = subprocess.run(
            (str(executable),),
            executable=str(launch_executable),
            pass_fds=guard.pass_fds,
            cwd=tmp_path,
            env=sterile_git_environment({"PATH": os.environ.get("PATH", "")}),
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    assert result.returncode == 0
    assert result.stdout.strip() == "snapshot-trusted"
    assert executable.read_text(encoding="utf-8") == replacement


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable snapshot only")
def test_posix_typed_runner_survives_same_inode_mutation_at_popen(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "runner"
    trusted = "#!/bin/sh\nprintf 'typed-snapshot-trusted\\n'\n"
    replacement = "#!/bin/sh\nprintf 'typed-same-inode-replacement\\n'\n"
    executable.write_text(trusted, encoding="utf-8")
    executable.chmod(0o700)
    original_stat = executable.stat()
    original_popen = execution_module.subprocess.Popen
    mutated = False

    def mutating_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        nonlocal mutated
        if not mutated:
            executable.write_text(replacement, encoding="utf-8")
            mutated = True
        return original_popen(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(execution_module.subprocess, "Popen", mutating_popen)

    result = run_typed_process(
        (str(executable),),
        process_policy=ProcessPolicy((str(executable),)),
        resource_budget=ResourceBudget(
            timeout_seconds=5,
            max_output_bytes=4096,
            max_changed_files=1,
        ),
        cwd=tmp_path,
        environment=sterile_git_environment(
            {"PATH": os.environ.get("PATH", "")}
        ),
    )

    mutated_stat = executable.stat()
    assert mutated is True
    assert (mutated_stat.st_dev, mutated_stat.st_ino) == (
        original_stat.st_dev,
        original_stat.st_ino,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "typed-snapshot-trusted"
    assert executable.read_text(encoding="utf-8") == replacement


@pytest.mark.skipif(
    os.name == "nt"
    or getattr(os, "memfd_create", None) is None
    or not getattr(os, "MFD_ALLOW_SEALING", 0),
    reason="Linux sealed memfd only",
)
def test_posix_launch_snapshot_is_write_sealed(tmp_path: pathlib.Path) -> None:
    executable = tmp_path / "runner"
    executable.write_text(
        "#!/bin/sh\nprintf 'sealed-snapshot\\n'\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)

    guard = execution_module._PinnedExecutableLaunchGuard(executable, ())
    with guard:
        assert len(guard.pass_fds) == 1
        descriptor = guard.pass_fds[0]
        with pytest.raises(OSError):
            os.pwrite(descriptor, b"x", 0)


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable snapshot only")
def test_posix_launch_snapshot_falls_back_without_memfd(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "runner"
    executable.write_text(
        "#!/bin/sh\nprintf 'fallback-snapshot\\n'\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    monkeypatch.setattr(execution_module.os, "memfd_create", None, raising=False)

    guard = execution_module._PinnedExecutableLaunchGuard(executable, ())
    with guard as launch_executable:
        assert len(guard.pass_fds) == 1
        with pytest.raises(OSError):
            os.pwrite(guard.pass_fds[0], b"x", 0)
        result = subprocess.run(
            (str(executable),),
            executable=str(launch_executable),
            pass_fds=guard.pass_fds,
            cwd=tmp_path,
            env=sterile_git_environment({"PATH": os.environ.get("PATH", "")}),
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    assert result.returncode == 0
    assert result.stdout.strip() == "fallback-snapshot"


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable snapshot only")
def test_posix_launch_snapshot_does_not_grant_execute_permission(
    tmp_path: pathlib.Path,
) -> None:
    executable = tmp_path / "runner"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o600)

    guard = execution_module._PinnedExecutableLaunchGuard(executable, ())
    with pytest.raises(
        execution_module.ProcessExecutionError,
        match="pinned runtime executable is not executable",
    ):
        with guard:
            raise AssertionError("non-executable source must not gain launch permission")


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable snapshot only")
def test_posix_execute_permission_check_stays_on_held_inode(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "runner"
    replacement = tmp_path / "replacement"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o600)
    replacement.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    replacement.chmod(0o700)
    original_inode = executable.stat().st_ino
    original_access = execution_module.os.access
    replaced = False

    def swapping_access(path: object, mode: int, *args: object, **kwargs: object) -> bool:
        nonlocal replaced
        if not replaced and mode == os.X_OK:
            os.replace(replacement, executable)
            replaced = True
        return original_access(path, mode, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(execution_module.os, "access", swapping_access)

    guard = execution_module._PinnedExecutableLaunchGuard(executable, ())
    with pytest.raises(
        execution_module.ProcessExecutionError,
        match="pinned runtime executable is not executable",
    ):
        with guard:
            raise AssertionError("replacement permissions must not authorize held bytes")

    assert replaced is True
    assert executable.stat().st_ino != original_inode


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable snapshot only")
def test_posix_snapshot_reopen_rejects_writable_dup_semantics(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = tmp_path / "snapshot"
    descriptor = os.open(
        snapshot,
        os.O_RDWR | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    duplicated: list[int] = []
    original_open = execution_module.os.open

    def duplicating_open(
        path: object,
        flags: int,
        *args: object,
        **kwargs: object,
    ) -> int:
        value = os.fspath(path)
        if value.startswith("/proc/self/fd/") or value.startswith("/dev/fd/"):
            duplicate = os.dup(descriptor)
            duplicated.append(duplicate)
            return duplicate
        return original_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(execution_module.os, "open", duplicating_open)
    try:
        with pytest.raises(
            execution_module.ProcessExecutionError,
            match="read-only runtime executable snapshot descriptor is unavailable",
        ):
            execution_module._reopen_posix_snapshot_read_only(descriptor)
    finally:
        os.close(descriptor)

    assert duplicated
    for duplicate in duplicated:
        with pytest.raises(OSError):
            os.fstat(duplicate)


def test_typed_runner_rejects_same_path_replacement_after_runtime_admission(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / pathlib.Path(sys.executable).name
    replacement = tmp_path / "replacement"
    shutil.copy2(pathlib.Path(sys.executable).resolve(strict=True), executable)
    replacement.write_bytes(b"replacement executable bytes")
    original_admit = execution_module._pinned_runtime_argv
    replaced = False
    popen_called = False

    def admit_then_replace(
        argv: object,
        allowed_executables: object,
    ) -> object:
        nonlocal replaced
        admission = original_admit(
            argv,  # type: ignore[arg-type]
            allowed_executables,  # type: ignore[arg-type]
        )
        os.replace(replacement, executable)
        replaced = True
        return admission

    def forbidden_popen(*args: object, **kwargs: object) -> object:
        nonlocal popen_called
        popen_called = True
        raise AssertionError("replaced executable reached Popen")

    monkeypatch.setattr(
        execution_module,
        "_pinned_runtime_argv",
        admit_then_replace,
    )
    monkeypatch.setattr(execution_module.subprocess, "Popen", forbidden_popen)

    with pytest.raises(
        execution_module.ProcessExecutionError,
        match="bytes changed before process launch",
    ):
        run_typed_process(
            (str(executable),),
            process_policy=ProcessPolicy((str(executable),)),
            resource_budget=ResourceBudget(
                timeout_seconds=5,
                max_output_bytes=4096,
                max_changed_files=1,
            ),
            cwd=tmp_path,
            environment=sterile_git_environment(
                {"PATH": os.environ.get("PATH", "")}
            ),
        )

    assert replaced is True
    assert popen_called is False


def test_git_launch_rejects_same_path_change_against_earlier_digest(
    tmp_path: pathlib.Path,
) -> None:
    executable = tmp_path / "git"
    replacement = tmp_path / "replacement"
    executable.write_bytes(b"trusted git bytes")
    replacement.write_bytes(b"replacement git bytes")
    expected_sha256 = execution_module._pinned_executable_sha256(executable)
    os.replace(replacement, executable)

    with pytest.raises(
        WorkspaceSecurityError,
        match="executable authority changed before launch",
    ):
        execution_module._git(
            (str(executable), "--version"),
            cwd=tmp_path,
            environment={},
            expected_executable_sha256=expected_sha256,
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor launch only")
def test_posix_absolute_git_launch_owns_descriptor_guard(
    tmp_path: pathlib.Path,
) -> None:
    git_executable = shutil.which("git")
    if git_executable is None:
        pytest.skip("Git CLI unavailable")
    executable = pathlib.Path(git_executable).resolve(strict=True)
    expected_sha256 = execution_module._pinned_executable_sha256(executable)

    result = execution_module._git(
        (str(executable), "--version"),
        cwd=tmp_path,
        environment=sterile_git_environment(
            {"PATH": os.environ.get("PATH", "")}
        ),
        expected_executable_sha256=expected_sha256,
    )

    assert result.returncode == 0
    assert result.stdout.startswith("git version ")


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable snapshot only")
def test_posix_git_launch_survives_same_inode_mutation_at_subprocess_run(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_git = shutil.which("git")
    if source_git is None:
        pytest.skip("Git CLI unavailable")
    executable = tmp_path / pathlib.Path(source_git).name
    shutil.copy2(pathlib.Path(source_git).resolve(strict=True), executable)
    expected_sha256 = execution_module._pinned_executable_sha256(executable)
    original_stat = executable.stat()
    original_run = execution_module.subprocess.run
    mutated = False

    def mutating_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal mutated
        if not mutated:
            executable.write_bytes(b"mutated git executable")
            mutated = True
        return original_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(execution_module.subprocess, "run", mutating_run)

    result = execution_module._git(
        (str(executable), "--version"),
        cwd=tmp_path,
        environment=sterile_git_environment(
            {"PATH": os.environ.get("PATH", "")}
        ),
        expected_executable_sha256=expected_sha256,
    )

    mutated_stat = executable.stat()
    assert mutated is True
    assert (mutated_stat.st_dev, mutated_stat.st_ino) == (
        original_stat.st_dev,
        original_stat.st_ino,
    )
    assert result.returncode == 0
    assert result.stdout.startswith("git version ")
    assert executable.read_bytes() == b"mutated git executable"


def test_private_git_rejects_replacement_after_host_git_admission(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, base_sha = _make_source_repository(tmp_path)
    job_root = tmp_path / "jobs" / "job-admission-swap"
    job_root.mkdir(parents=True)
    plan = make_sterile_git_plan(
        repository_root=repository,
        job_root=job_root,
        branch_name="toolsmith/job-admission-swap",
        base_sha=base_sha,
        source_environment={"PATH": os.environ.get("PATH", "")},
    )
    source_git = shutil.which("git")
    if source_git is None:
        pytest.skip("Git CLI unavailable")
    executable = tmp_path / pathlib.Path(source_git).name
    replacement = tmp_path / "replacement-git"
    shutil.copy2(pathlib.Path(source_git).resolve(strict=True), executable)
    replacement.write_bytes(b"replacement git bytes")
    original_resolve = execution_module._resolve_host_git_executable
    replaced = False
    run_called = False

    def resolve_then_replace(git_executable: str) -> object:
        nonlocal replaced
        admission = original_resolve(git_executable)
        os.replace(replacement, executable)
        replaced = True
        return admission

    def forbidden_run(*args: object, **kwargs: object) -> object:
        nonlocal run_called
        run_called = True
        raise AssertionError("replaced Git executable reached subprocess.run")

    monkeypatch.setattr(
        execution_module,
        "_resolve_host_git_executable",
        resolve_then_replace,
    )
    monkeypatch.setattr(execution_module.subprocess, "run", forbidden_run)

    with pytest.raises(
        WorkspaceSecurityError,
        match="executable authority changed before launch",
    ):
        prepare_private_git_workspace(plan, git_executable=str(executable))

    assert replaced is True
    assert run_called is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor identity only")
def test_posix_executable_admission_rejects_path_swap_after_descriptor_open(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "runner"
    replacement = tmp_path / "replacement"
    executable.write_bytes(b"trusted executable bytes")
    replacement.write_bytes(b"replacement executable bytes")
    original_resolve = execution_module._resolve_pinned_executable
    calls = 0

    def swapping_resolve(
        candidate: pathlib.Path,
        arguments: tuple[str, ...],
    ) -> pathlib.Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            os.replace(replacement, executable)
        return original_resolve(candidate, arguments)

    monkeypatch.setattr(
        execution_module,
        "_resolve_pinned_executable",
        swapping_resolve,
    )

    with pytest.raises(
        execution_module.ProcessExecutionError,
        match="changed before process launch",
    ):
        execution_module._admit_pinned_executable(executable, ())

    assert calls == 2
    assert executable.read_bytes() == b"replacement executable bytes"


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics only")
def test_windows_executable_admission_holds_lock_through_readmission(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "runner.exe"
    replacement = tmp_path / "replacement.exe"
    executable.write_bytes(b"trusted executable bytes")
    replacement.write_bytes(b"replacement executable bytes")
    original_resolve = execution_module._resolve_pinned_executable
    original_hash = execution_module._pinned_executable_sha256
    calls = 0
    replacement_blocked = False
    hash_replacement_blocked = False

    def probing_resolve(
        candidate: pathlib.Path,
        arguments: tuple[str, ...],
    ) -> pathlib.Path:
        nonlocal calls, replacement_blocked
        calls += 1
        if calls == 2:
            with pytest.raises(OSError):
                os.replace(replacement, executable)
            replacement_blocked = True
        return original_resolve(candidate, arguments)

    def probing_hash(candidate: pathlib.Path) -> str:
        nonlocal hash_replacement_blocked
        with pytest.raises(OSError):
            os.replace(replacement, executable)
        hash_replacement_blocked = True
        return original_hash(candidate)

    monkeypatch.setattr(
        execution_module,
        "_resolve_pinned_executable",
        probing_resolve,
    )
    monkeypatch.setattr(
        execution_module,
        "_pinned_executable_sha256",
        probing_hash,
    )

    admission = execution_module._admit_pinned_executable(executable, ())

    assert calls == 2
    assert replacement_blocked is True
    assert hash_replacement_blocked is True
    assert admission.sha256 == original_hash(executable)
    assert replacement.exists()

    os.replace(replacement, executable)
    assert executable.read_bytes() == b"replacement executable bytes"


@pytest.mark.skipif(os.name != "nt", reason="Windows CreateProcess boundary only")
def test_windows_typed_runner_holds_launch_paths_during_popen(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = shutil.which("whoami.exe") or shutil.which("whoami")
    if helper is None:
        pytest.skip("standalone Windows helper unavailable")
    source = pathlib.Path(helper).resolve(strict=True)
    executable = tmp_path / "runner.exe"
    replacement = tmp_path / "replacement.exe"
    workspace = tmp_path / "workspace"
    cwd = workspace / "cwd"
    moved_cwd = workspace / "moved-cwd"
    moved_workspace = tmp_path / "moved-workspace"
    workspace.mkdir()
    cwd.mkdir()
    shutil.copy2(source, executable)
    replacement.write_bytes(b"replacement executable bytes")
    original_popen = execution_module.subprocess.Popen
    attempted = False

    def probing_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        nonlocal attempted
        attempted = True
        with pytest.raises(OSError):
            os.replace(replacement, executable)
        with pytest.raises(OSError):
            os.replace(cwd, moved_cwd)
        with pytest.raises(OSError):
            os.replace(workspace, moved_workspace)
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(execution_module.subprocess, "Popen", probing_popen)

    result = run_typed_process(
        (str(executable),),
        process_policy=ProcessPolicy((str(executable),)),
        resource_budget=ResourceBudget(
            timeout_seconds=5,
            max_output_bytes=4096,
            max_changed_files=1,
        ),
        cwd=cwd,
        workspace_root=workspace,
        environment=sterile_git_environment(
            {"PATH": os.environ.get("PATH", "")}
        ),
    )

    assert attempted is True
    assert result.returncode == 0
    assert result.stdout.strip()
    assert replacement.exists()
    assert cwd.is_dir()

    os.replace(replacement, executable)
    os.replace(cwd, moved_cwd)
    os.replace(workspace, moved_workspace)
    assert executable.read_bytes() == b"replacement executable bytes"
    assert (moved_workspace / "moved-cwd").is_dir()


def test_typed_runner_rechecks_cancellation_at_final_launch_boundary(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancellation = threading.Event()
    original_guard = execution_module._PinnedExecutableLaunchGuard
    popen_called = False

    class CancellingGuard(original_guard):
        def __enter__(self) -> pathlib.Path:
            resolved = super().__enter__()
            cancellation.set()
            return resolved

    def forbidden_popen(*args: object, **kwargs: object) -> object:
        nonlocal popen_called
        popen_called = True
        raise AssertionError("Popen crossed a cancelled final launch boundary")

    monkeypatch.setattr(
        execution_module,
        "_PinnedExecutableLaunchGuard",
        CancellingGuard,
    )
    monkeypatch.setattr(execution_module.subprocess, "Popen", forbidden_popen)

    result = run_typed_process(
        (sys.executable, "-c", "print('must not run')"),
        process_policy=_python_process_policy(),
        resource_budget=ResourceBudget(
            timeout_seconds=5,
            max_output_bytes=4096,
            max_changed_files=1,
        ),
        cwd=tmp_path,
        environment=sterile_git_environment(
            {"PATH": os.environ.get("PATH", "")}
        ),
        cancellation_event=cancellation,
    )

    assert result.cancelled is True
    assert result.returncode != 0
    assert popen_called is False


def test_typed_runner_rechecks_deadline_at_final_launch_boundary(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monotonic_calls = 0
    popen_called = False

    class FakeTime:
        @staticmethod
        def monotonic() -> float:
            nonlocal monotonic_calls
            monotonic_calls += 1
            return 0.0 if monotonic_calls == 1 else 2.0

        @staticmethod
        def sleep(_seconds: float) -> None:
            raise AssertionError("sleep must not occur before rejected launch")

    def forbidden_popen(*args: object, **kwargs: object) -> object:
        nonlocal popen_called
        popen_called = True
        raise AssertionError("Popen crossed an expired final launch boundary")

    monkeypatch.setattr(execution_module, "time", FakeTime())
    monkeypatch.setattr(execution_module.subprocess, "Popen", forbidden_popen)

    result = run_typed_process(
        (sys.executable, "-c", "print('must not run')"),
        process_policy=_python_process_policy(),
        resource_budget=ResourceBudget(
            timeout_seconds=1,
            max_output_bytes=4096,
            max_changed_files=1,
        ),
        cwd=tmp_path,
        environment=sterile_git_environment(
            {"PATH": os.environ.get("PATH", "")}
        ),
    )

    assert result.timed_out is True
    assert result.returncode != 0
    assert popen_called is False
    assert monotonic_calls >= 2


def test_typed_runner_uses_final_executable_launch_guard(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[pathlib.Path] = []
    original_guard = execution_module._PinnedExecutableLaunchGuard

    class RecordingGuard(original_guard):
        def __enter__(self) -> pathlib.Path:
            resolved = super().__enter__()
            observed.append(resolved)
            return resolved

    monkeypatch.setattr(
        execution_module,
        "_PinnedExecutableLaunchGuard",
        RecordingGuard,
    )

    result = run_typed_process(
        (sys.executable, "-c", "print('guarded')"),
        process_policy=_python_process_policy(),
        resource_budget=ResourceBudget(
            timeout_seconds=5,
            max_output_bytes=4096,
            max_changed_files=1,
        ),
        cwd=tmp_path,
        environment=sterile_git_environment({"PATH": os.environ.get("PATH", "")}),
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "guarded"
    assert len(observed) == 1
    assert observed[0] == pathlib.Path(sys.executable).resolve(strict=True)


def test_private_git_workspace_refuses_ambiguous_reuse(tmp_path: pathlib.Path) -> None:
    repository, base_sha = _make_source_repository(tmp_path)
    job_root = tmp_path / "jobs" / "job-1"
    job_root.mkdir(parents=True)
    plan = make_sterile_git_plan(
        repository_root=repository,
        job_root=job_root,
        branch_name="toolsmith/job-1",
        base_sha=base_sha,
        source_environment={"PATH": os.environ.get("PATH", "")},
    )
    plan.private_git_dir.mkdir()

    with pytest.raises(WorkspaceSecurityError, match="ambiguous reuse"):
        prepare_private_git_workspace(plan)


def test_typed_runner_preserves_literal_arguments_and_captures_output(
    tmp_path: pathlib.Path,
) -> None:
    argument = "value;still-literal"
    result = run_typed_process(
        (sys.executable, "-c", "import sys; print(sys.argv[1])", argument),
        process_policy=_python_process_policy(),
        resource_budget=ResourceBudget(
            timeout_seconds=5,
            max_output_bytes=4096,
            max_changed_files=1,
        ),
        cwd=tmp_path,
        environment=sterile_git_environment({"PATH": os.environ.get("PATH", "")}),
    )

    assert result.returncode == 0
    assert result.stdout.strip() == argument
    assert result.stderr == ""
    assert not result.timed_out
    assert not result.cancelled
    assert not result.output_limit_exceeded
    expected_isolation = (
        IsolationClass.PROCESS_CONTAINED if os.name == "nt" else IsolationClass.POLICY_ONLY
    )
    assert result.isolation_class is expected_isolation


def test_typed_runner_kills_on_timeout(tmp_path: pathlib.Path) -> None:
    result = run_typed_process(
        (sys.executable, "-c", "import time; time.sleep(30)"),
        process_policy=_python_process_policy(),
        resource_budget=ResourceBudget(
            timeout_seconds=1,
            max_output_bytes=4096,
            max_changed_files=1,
        ),
        cwd=tmp_path,
        environment=sterile_git_environment({"PATH": os.environ.get("PATH", "")}),
    )

    assert result.timed_out
    assert result.returncode != 0


def test_typed_runner_honors_cancellation(tmp_path: pathlib.Path) -> None:
    cancellation = threading.Event()
    cancellation.set()
    result = run_typed_process(
        (sys.executable, "-c", "import time; time.sleep(30)"),
        process_policy=_python_process_policy(),
        resource_budget=ResourceBudget(
            timeout_seconds=5,
            max_output_bytes=4096,
            max_changed_files=1,
        ),
        cwd=tmp_path,
        environment=sterile_git_environment({"PATH": os.environ.get("PATH", "")}),
        cancellation_event=cancellation,
    )

    assert result.cancelled
    assert result.returncode != 0


def test_typed_runner_kills_on_output_limit(tmp_path: pathlib.Path) -> None:
    result = run_typed_process(
        (sys.executable, "-c", "print('x' * 20000)"),
        process_policy=_python_process_policy(),
        resource_budget=ResourceBudget(
            timeout_seconds=5,
            max_output_bytes=1024,
            max_changed_files=1,
        ),
        cwd=tmp_path,
        environment=sterile_git_environment({"PATH": os.environ.get("PATH", "")}),
    )

    assert result.output_limit_exceeded
    assert len(result.stdout.encode("utf-8")) <= 1024
