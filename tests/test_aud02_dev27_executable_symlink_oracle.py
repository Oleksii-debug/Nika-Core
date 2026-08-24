"""AUD02 QA_ONLY oracle for DEV27 executable-identity containment."""

from __future__ import annotations

import os
import pathlib

import pytest

from nika_core.toolsmith import ProcessPolicy, ResourceBudget
from nika_core.toolsmith.execution import ProcessExecutionError, run_typed_process
from nika_core.toolsmith.workspace_security import WorkspaceSecurityError


def test_allowlisted_executable_symlink_cannot_resolve_to_forbidden_shell(
    tmp_path: pathlib.Path,
) -> None:
    if os.name == "nt":
        pytest.skip("portable oracle uses POSIX /bin/sh; Windows reparse proof remains separate")

    shell = pathlib.Path("/bin/sh")
    if not shell.exists():
        pytest.skip("POSIX shell fixture unavailable")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    alias = tmp_path / "safe-runner"
    alias.symlink_to(shell)
    marker = workspace / "shell-bypass.txt"

    budget = ResourceBudget(
        timeout_seconds=5,
        max_output_bytes=4096,
        max_changed_files=1,
    )

    with pytest.raises((WorkspaceSecurityError, ProcessExecutionError)):
        run_typed_process(
            (str(alias), "-c", "printf bypass > shell-bypass.txt"),
            process_policy=ProcessPolicy((str(alias),)),
            resource_budget=budget,
            cwd=workspace,
            workspace_root=workspace,
            environment=dict(os.environ),
        )

    assert not marker.exists(), "forbidden shell target executed through allowlisted symlink alias"
