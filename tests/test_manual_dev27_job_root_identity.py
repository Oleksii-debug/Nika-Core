from __future__ import annotations

import pathlib

import pytest

from nika_core.toolsmith import execution
from nika_core.toolsmith.execution import (
    cleanup_private_git_workspace,
    prepare_private_git_workspace,
)
from nika_core.toolsmith.workspace_security import (
    WorkspaceSecurityError,
    make_sterile_git_plan,
)


def _replace_job_root_with_symlink(
    job_root: pathlib.Path,
    external: pathlib.Path,
) -> pathlib.Path:
    original = job_root.with_name(f"{job_root.name}-original")
    job_root.rename(original)
    try:
        job_root.symlink_to(external, target_is_directory=True)
    except OSError:
        original.rename(job_root)
        pytest.skip("directory symlink creation unavailable on this host")
    return original


def test_plan_rejects_initial_symlink_job_root_before_canonicalization(
    tmp_path: pathlib.Path,
) -> None:
    repository = tmp_path / "production"
    repository.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    job_root = tmp_path / "job-link"
    try:
        job_root.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation unavailable on this host")

    with pytest.raises(WorkspaceSecurityError, match="job workspace root"):
        make_sterile_git_plan(
            repository_root=repository,
            job_root=job_root,
            branch_name="toolsmith/dev27-initial-root-link",
            base_sha="0" * 40,
        )


def test_plan_requires_precreated_job_root(tmp_path: pathlib.Path) -> None:
    repository = tmp_path / "production"
    repository.mkdir()
    missing_job_root = tmp_path / "jobs" / "missing-job"

    with pytest.raises(WorkspaceSecurityError, match="job workspace root must exist"):
        make_sterile_git_plan(
            repository_root=repository,
            job_root=missing_job_root,
            branch_name="toolsmith/dev27-missing-root",
            base_sha="1" * 40,
        )


def test_prepare_refuses_replaced_job_root_before_git_or_external_write(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "production"
    repository.mkdir()
    (repository / ".git").mkdir()
    job_root = tmp_path / "jobs" / "job-1"
    job_root.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    marker = external / "keep.txt"
    marker.write_text("external\n", encoding="utf-8")

    plan = make_sterile_git_plan(
        repository_root=repository,
        job_root=job_root,
        branch_name="toolsmith/dev27-job-root-prepare",
        base_sha="a" * 40,
    )
    original = _replace_job_root_with_symlink(job_root, external)

    def _must_not_run_git(*args: object, **kwargs: object) -> object:
        raise AssertionError("Git must not run through an attacker-replaced job root")

    monkeypatch.setattr(execution, "_git", _must_not_run_git)

    with pytest.raises(WorkspaceSecurityError, match="job workspace root"):
        prepare_private_git_workspace(plan)

    assert marker.read_text(encoding="utf-8") == "external\n"
    assert not (external / "_nika_private_git").exists()
    assert not (external / "worktree").exists()
    assert original.is_dir()


def test_cleanup_refuses_replaced_job_root_and_preserves_external_tree(
    tmp_path: pathlib.Path,
) -> None:
    repository = tmp_path / "production"
    repository.mkdir()
    job_root = tmp_path / "jobs" / "job-1"
    job_root.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()

    plan = make_sterile_git_plan(
        repository_root=repository,
        job_root=job_root,
        branch_name="toolsmith/dev27-job-root-cleanup",
        base_sha="b" * 40,
    )

    original = _replace_job_root_with_symlink(job_root, external)
    external_private = external / "_nika_private_git"
    external_worktree = external / "worktree"
    external_private.mkdir()
    external_worktree.mkdir()
    private_marker = external_private / "keep-private.txt"
    worktree_marker = external_worktree / "keep-worktree.txt"
    private_marker.write_text("private\n", encoding="utf-8")
    worktree_marker.write_text("worktree\n", encoding="utf-8")

    with pytest.raises(WorkspaceSecurityError, match="job workspace root"):
        cleanup_private_git_workspace(plan)

    assert private_marker.read_text(encoding="utf-8") == "private\n"
    assert worktree_marker.read_text(encoding="utf-8") == "worktree\n"
    assert external_private.is_dir()
    assert external_worktree.is_dir()
    assert original.is_dir()
