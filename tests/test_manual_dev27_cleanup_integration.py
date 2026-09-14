from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest

from nika_core.toolsmith.execution import (
    cleanup_private_git_workspace,
    prepare_private_git_workspace,
)
from nika_core.toolsmith.workspace_security import make_sterile_git_plan


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


def _production_repository(tmp_path: pathlib.Path) -> tuple[pathlib.Path, str]:
    if shutil.which("git") is None:
        pytest.skip("Git CLI unavailable")
    repository = tmp_path / "production"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Nika DEV27 Test")
    _git(repository, "config", "user.email", "nika-dev27@example.invalid")
    (repository / "README.md").write_text("production\n", encoding="utf-8")
    source = repository / "src"
    source.mkdir()
    (source / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repository, "add", "README.md", "src/module.py")
    _git(repository, "commit", "-m", "base")
    return repository, _git(repository, "rev-parse", "HEAD")


def test_cleanup_real_private_git_workspace_preserves_production(tmp_path: pathlib.Path) -> None:
    repository, base_sha = _production_repository(tmp_path)
    job_root = tmp_path / "jobs" / "job-1"
    job_root.mkdir(parents=True)
    sentinel = job_root / "operator-note.txt"
    sentinel.write_text("keep\n", encoding="utf-8")
    plan = make_sterile_git_plan(
        repository_root=repository,
        job_root=job_root,
        branch_name="toolsmith/dev27-cleanup-proof",
        base_sha=base_sha,
        source_environment={"PATH": os.environ.get("PATH", "")},
    )

    prepared = prepare_private_git_workspace(plan)
    assert prepared.head_sha == base_sha
    assert plan.private_git_dir.exists()
    assert plan.worktree_root.exists()

    cleanup_private_git_workspace(plan)

    assert not plan.private_git_dir.exists()
    assert not plan.worktree_root.exists()
    assert sentinel.read_text(encoding="utf-8") == "keep\n"
    assert _git(repository, "rev-parse", "HEAD") == base_sha
    assert (repository / "README.md").read_text(encoding="utf-8") == "production\n"
    assert (repository / "src" / "module.py").read_text(encoding="utf-8") == "VALUE = 1\n"
