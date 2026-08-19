from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import threading

import pytest

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
        process_policy=ProcessPolicy((sys.executable,)),
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
        process_policy=ProcessPolicy((sys.executable,)),
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
        process_policy=ProcessPolicy((sys.executable,)),
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
        process_policy=ProcessPolicy((sys.executable,)),
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
