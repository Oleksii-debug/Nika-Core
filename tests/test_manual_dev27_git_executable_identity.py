from __future__ import annotations

import os
import pathlib
import sys

import pytest

import nika_core.toolsmith.execution as execution
from nika_core.toolsmith.execution import prepare_private_git_workspace
from nika_core.toolsmith.workspace_security import (
    WorkspaceSecurityError,
    make_sterile_git_plan,
)


def test_host_git_discovery_does_not_use_worker_path(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "production"
    repository.mkdir()
    (repository / ".git").mkdir()
    job_root = tmp_path / "job"
    job_root.mkdir()
    worker_bin = tmp_path / "worker-bin"
    worker_bin.mkdir()

    plan = make_sterile_git_plan(
        repository_root=repository,
        job_root=job_root,
        branch_name="toolsmith/dev27-host-git",
        base_sha="a" * 40,
        source_environment={"PATH": str(worker_bin)},
    )
    observed: dict[str, str] = {}

    def _trusted_which(command: str, *, path: str | None = None) -> str:
        observed["command"] = command
        observed["path"] = "" if path is None else path
        return sys.executable

    def _capture_first_git(
        argv: tuple[str, ...],
        *,
        cwd: pathlib.Path,
        environment: object,
        timeout_seconds: int = 60,
    ) -> object:
        del cwd, environment, timeout_seconds
        observed["argv0"] = argv[0]
        raise WorkspaceSecurityError("stop after pinned Git identity proof")

    monkeypatch.setattr(execution.shutil, "which", _trusted_which)
    monkeypatch.setattr(execution, "_git", _capture_first_git)

    with pytest.raises(WorkspaceSecurityError, match="pinned Git identity proof"):
        prepare_private_git_workspace(plan)

    assert plan.environment["PATH"] == str(worker_bin)
    assert observed["command"] == "git"
    assert observed["path"] == os.environ.get("PATH", "")
    assert pathlib.Path(observed["argv0"]) == pathlib.Path(sys.executable).resolve(strict=True)
    assert observed["argv0"] != "git"


def test_relative_path_qualified_git_executable_is_rejected() -> None:
    with pytest.raises(WorkspaceSecurityError, match="relative path-qualified"):
        execution._resolve_host_git_executable("tools/git")


def test_absolute_git_executable_is_canonicalized_without_path_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _must_not_search(*args: object, **kwargs: object) -> str:
        raise AssertionError("absolute Git identity must not use PATH discovery")

    monkeypatch.setattr(execution.shutil, "which", _must_not_search)
    resolved = execution._resolve_host_git_executable(sys.executable)
    assert pathlib.Path(resolved) == pathlib.Path(sys.executable).resolve(strict=True)
