"""AUD02 QA_ONLY oracles for DEV27 executable-identity containment."""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

from nika_core.toolsmith import ProcessPolicy, ResourceBudget
from nika_core.toolsmith.execution import ProcessExecutionError, run_typed_process
from nika_core.toolsmith.workspace_security import WorkspaceSecurityError, validate_typed_argv


def _budget() -> ResourceBudget:
    return ResourceBudget(timeout_seconds=5, max_output_bytes=4096, max_changed_files=1)


def test_exact_allowlist_still_cannot_authorize_windows_batch_entrypoint() -> None:
    command = r"C:\trusted\build.cmd"
    with pytest.raises(WorkspaceSecurityError):
        validate_typed_argv((command, "arg"), (command,))


def test_allowlisted_executable_symlink_cannot_resolve_to_forbidden_shell(
    tmp_path: pathlib.Path,
) -> None:
    if os.name == "nt":
        pytest.skip("portable oracle uses POSIX /bin/sh; physical Windows reparse proof is separate")

    shell = pathlib.Path("/bin/sh")
    if not shell.exists():
        pytest.skip("POSIX shell fixture unavailable")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    alias = tmp_path / "safe-runner"
    alias.symlink_to(shell)
    marker = workspace / "shell-bypass.txt"

    with pytest.raises((WorkspaceSecurityError, ProcessExecutionError)):
        run_typed_process(
            (str(alias), "-c", "printf bypass > shell-bypass.txt"),
            process_policy=ProcessPolicy((str(alias),)),
            resource_budget=_budget(),
            cwd=workspace,
            workspace_root=workspace,
            environment=dict(os.environ),
        )

    assert not marker.exists(), "forbidden shell target executed through allowlisted symlink alias"


def test_allowlisted_alias_cannot_substitute_untrusted_non_shell_executable(
    tmp_path: pathlib.Path,
) -> None:
    if os.name == "nt":
        pytest.skip("portable symlink oracle; physical Windows reparse proof is separate")

    python = pathlib.Path(sys.executable).resolve(strict=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    alias = tmp_path / "trusted-tool"
    alias.symlink_to(python)
    marker = workspace / "canonical-substitution.txt"

    command = "from pathlib import Path; Path('canonical-substitution.txt').write_text('bypass')"
    with pytest.raises((WorkspaceSecurityError, ProcessExecutionError)):
        run_typed_process(
            (str(alias), "-c", command),
            process_policy=ProcessPolicy((str(alias),)),
            resource_budget=_budget(),
            cwd=workspace,
            workspace_root=workspace,
            environment=dict(os.environ),
        )

    assert not marker.exists(), "untrusted canonical executable ran through trusted alias"
