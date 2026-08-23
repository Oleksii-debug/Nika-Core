from __future__ import annotations

import os
import pathlib
import sys

import pytest

from nika_core.toolsmith import ProcessPolicy, ResourceBudget
from nika_core.toolsmith.execution import (
    ProcessExecutionError,
    cleanup_private_git_workspace,
    run_typed_process,
)
from nika_core.toolsmith.workspace_security import (
    WorkspacePathPolicy,
    WorkspaceSecurityError,
    collect_tree_delta_evidence,
    collect_tree_evidence,
    make_sterile_git_plan,
    validate_typed_argv,
)


def _budget(*, max_changed_files: int = 4) -> ResourceBudget:
    return ResourceBudget(
        timeout_seconds=5,
        max_output_bytes=4096,
        max_changed_files=max_changed_files,
    )


@pytest.mark.parametrize(
    "spoofed",
    (
        "tools/python.exe",
        "tools\\python.exe",
        "../python.exe",
        "C:/attacker/python.exe",
        "C:\\attacker\\python.exe",
    ),
)
def test_executable_basename_cannot_spoof_allowlisted_identity(spoofed: str) -> None:
    with pytest.raises(WorkspaceSecurityError, match="exactly allowlisted"):
        validate_typed_argv((spoofed, "-c", "pass"), {"python.exe"})


def test_runtime_rejects_bare_executable_search_order(tmp_path: pathlib.Path) -> None:
    executable_name = pathlib.Path(sys.executable).name
    with pytest.raises(ProcessExecutionError, match="absolute pinned path"):
        run_typed_process(
            (executable_name, "-c", "print('must not run')"),
            process_policy=ProcessPolicy((executable_name,)),
            resource_budget=_budget(),
            cwd=tmp_path,
            environment=dict(os.environ),
        )


def test_runner_strips_secrets_and_pins_temp_inside_workspace(tmp_path: pathlib.Path) -> None:
    workspace = tmp_path / "workspace"
    cwd = workspace / "worktree"
    cwd.mkdir(parents=True)
    source_environment = dict(os.environ)
    source_environment.update(
        {
            "GITHUB_TOKEN": "must-not-leak",
            "GH_TOKEN": "must-not-leak-either",
            "NIKA_PRIVATE_SECRET": "must-not-leak-custom",
            "TEMP": str(tmp_path / "outside-temp"),
            "TMP": str(tmp_path / "outside-tmp"),
        }
    )
    code = (
        "import os; "
        "print(os.environ.get('GITHUB_TOKEN', '')); "
        "print(os.environ.get('GH_TOKEN', '')); "
        "print(os.environ.get('NIKA_PRIVATE_SECRET', '')); "
        "print(os.environ['TEMP'])"
    )

    result = run_typed_process(
        (sys.executable, "-c", code),
        process_policy=ProcessPolicy((sys.executable,)),
        resource_budget=_budget(),
        cwd=cwd,
        workspace_root=workspace,
        environment=source_environment,
    )

    assert result.returncode == 0
    lines = result.stdout.splitlines()
    assert lines[:3] == ["", "", ""]
    temp_root = pathlib.Path(lines[3]).resolve(strict=True)
    assert temp_root == (workspace / "_nika_process_tmp").resolve(strict=True)
    assert temp_root.is_relative_to(workspace.resolve(strict=True))


def test_runner_rejects_cwd_outside_declared_workspace(tmp_path: pathlib.Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()

    with pytest.raises(ProcessExecutionError, match="cwd escapes"):
        run_typed_process(
            (sys.executable, "-c", "print('must not run')"),
            process_policy=ProcessPolicy((sys.executable,)),
            resource_budget=_budget(),
            cwd=outside,
            workspace_root=workspace,
            environment=dict(os.environ),
        )


def test_workspace_plan_rejects_production_nested_inside_job_root(
    tmp_path: pathlib.Path,
) -> None:
    job_root = tmp_path / "job"
    repository = job_root / "production"
    repository.mkdir(parents=True)

    with pytest.raises(WorkspaceSecurityError, match="fully disjoint"):
        make_sterile_git_plan(
            repository_root=repository,
            job_root=job_root,
            branch_name="toolsmith/job-1",
            base_sha="a" * 40,
        )


def test_cleanup_removes_only_private_git_roots(tmp_path: pathlib.Path) -> None:
    repository = tmp_path / "production"
    job_root = tmp_path / "jobs" / "job-1"
    repository.mkdir()
    job_root.mkdir(parents=True)
    plan = make_sterile_git_plan(
        repository_root=repository,
        job_root=job_root,
        branch_name="toolsmith/job-1",
        base_sha="b" * 40,
    )
    plan.private_git_dir.mkdir()
    plan.worktree_root.mkdir()
    (plan.private_git_dir / "HEAD").write_text("ref: refs/heads/job\n", encoding="utf-8")
    (plan.worktree_root / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
    sentinel = job_root / "operator-note.txt"
    sentinel.write_text("keep\n", encoding="utf-8")

    cleanup_private_git_workspace(plan)

    assert not plan.private_git_dir.exists()
    assert not plan.worktree_root.exists()
    assert sentinel.read_text(encoding="utf-8") == "keep\n"


def test_cleanup_refuses_symlink_and_preserves_external_target(tmp_path: pathlib.Path) -> None:
    repository = tmp_path / "production"
    job_root = tmp_path / "jobs" / "job-1"
    external = tmp_path / "external"
    repository.mkdir()
    job_root.mkdir(parents=True)
    external.mkdir()
    marker = external / "keep.txt"
    marker.write_text("external\n", encoding="utf-8")
    plan = make_sterile_git_plan(
        repository_root=repository,
        job_root=job_root,
        branch_name="toolsmith/job-1",
        base_sha="c" * 40,
    )
    plan.private_git_dir.mkdir()
    plan.worktree_root.mkdir()
    link = plan.worktree_root / "escape"
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable on this host")

    with pytest.raises(WorkspaceSecurityError, match="cleanup refuses"):
        cleanup_private_git_workspace(plan)

    assert marker.read_text(encoding="utf-8") == "external\n"
    assert plan.private_git_dir.exists()
    assert plan.worktree_root.exists()


def test_tree_delta_proves_added_modified_and_deleted_files(tmp_path: pathlib.Path) -> None:
    before_root = tmp_path / "before"
    after_root = tmp_path / "after"
    (before_root / "src").mkdir(parents=True)
    (after_root / "src").mkdir(parents=True)
    (before_root / "src" / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
    (before_root / "src" / "b.py").write_text("DELETE = True\n", encoding="utf-8")
    (after_root / "src" / "a.py").write_text("VALUE = 2\n", encoding="utf-8")
    (after_root / "src" / "c.py").write_text("ADDED = True\n", encoding="utf-8")

    before = collect_tree_evidence(before_root)
    after = collect_tree_evidence(after_root)
    delta = collect_tree_delta_evidence(
        before,
        after,
        path_policy=WorkspacePathPolicy(("src",)),
        max_changed_files=3,
    )

    assert [(item.path, item.kind) for item in delta.changes] == [
        ("src/a.py", "modified"),
        ("src/b.py", "deleted"),
        ("src/c.py", "added"),
    ]
    assert delta.before_digest == before.digest
    assert delta.after_digest == after.digest
    assert len(delta.digest) == 64


def test_tree_delta_rejects_worker_control_plane_mutation(tmp_path: pathlib.Path) -> None:
    before_root = tmp_path / "before"
    after_root = tmp_path / "after"
    before_root.mkdir()
    workflow = after_root / ".github" / "workflows"
    workflow.mkdir(parents=True)
    (workflow / "pwn.yml").write_text("name: pwn\n", encoding="utf-8")

    before = collect_tree_evidence(before_root)
    after = collect_tree_evidence(after_root)
    with pytest.raises(WorkspaceSecurityError, match="control-plane"):
        collect_tree_delta_evidence(
            before,
            after,
            path_policy=WorkspacePathPolicy((".github",)),
            max_changed_files=1,
        )


def test_tree_delta_enforces_changed_file_budget(tmp_path: pathlib.Path) -> None:
    before_root = tmp_path / "before"
    after_root = tmp_path / "after"
    before_root.mkdir()
    source = after_root / "src"
    source.mkdir(parents=True)
    (source / "a.py").write_text("A = 1\n", encoding="utf-8")
    (source / "b.py").write_text("B = 1\n", encoding="utf-8")

    before = collect_tree_evidence(before_root)
    after = collect_tree_evidence(after_root)
    with pytest.raises(WorkspaceSecurityError, match="changed-file budget"):
        collect_tree_delta_evidence(
            before,
            after,
            path_policy=WorkspacePathPolicy(("src",)),
            max_changed_files=1,
        )
