from __future__ import annotations

import os
import pathlib
import sys

import pytest

from nika_core.toolsmith import ProcessPolicy, ResourceBudget, execution
from nika_core.toolsmith.execution import ProcessExecutionError, run_typed_process
from nika_core.toolsmith.workspace_security import WorkspaceSecurityError


def _budget() -> ResourceBudget:
    return ResourceBudget(
        timeout_seconds=5,
        max_output_bytes=4096,
        max_changed_files=1,
    )


def _make_symlink(link: pathlib.Path, target: pathlib.Path) -> None:
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symbolic-link creation is unavailable on this runner")


def test_allowlisted_symlink_chain_cannot_cross_forbidden_shell_name(
    tmp_path: pathlib.Path,
) -> None:
    if os.name == "nt":
        pytest.skip("portable shell-chain regression uses POSIX /bin/sh")
    shell = pathlib.Path("/bin/sh")
    if not shell.exists():
        pytest.skip("POSIX /bin/sh fixture unavailable")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    intermediate = tmp_path / "intermediate-runner"
    alias = tmp_path / "safe-runner"
    _make_symlink(intermediate, shell)
    _make_symlink(alias, intermediate)
    marker = workspace / "shell-bypass.txt"

    with pytest.raises(WorkspaceSecurityError, match="shell"):
        run_typed_process(
            (str(alias), "-c", "printf bypass > shell-bypass.txt"),
            process_policy=ProcessPolicy((str(alias),)),
            resource_budget=_budget(),
            cwd=workspace,
            workspace_root=workspace,
            environment=dict(os.environ),
        )

    assert not marker.exists()


def test_allowlisted_symlink_cannot_substitute_non_allowlisted_canonical_target(
    tmp_path: pathlib.Path,
) -> None:
    target = pathlib.Path(sys.executable).resolve(strict=True)
    alias = tmp_path / "safe-python"
    _make_symlink(alias, target)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = workspace / "canonical-substitution.txt"
    payload = (
        "from pathlib import Path; "
        "Path('canonical-substitution.txt').write_text('bad', encoding='utf-8')"
    )

    with pytest.raises(WorkspaceSecurityError, match="exactly allowlisted"):
        run_typed_process(
            (str(alias), "-c", payload),
            process_policy=ProcessPolicy((str(alias),)),
            resource_budget=_budget(),
            cwd=workspace,
            workspace_root=workspace,
            environment=dict(os.environ),
        )

    assert not marker.exists()


def test_safe_allowlisted_symlink_launches_canonical_target(
    tmp_path: pathlib.Path,
) -> None:
    target = pathlib.Path(sys.executable).resolve(strict=True)
    alias = tmp_path / "safe-python"
    _make_symlink(alias, target)
    canonical_target = alias.resolve(strict=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = run_typed_process(
        (str(alias), "-c", "print('canonical-ok')"),
        process_policy=ProcessPolicy((str(alias), str(canonical_target))),
        resource_budget=_budget(),
        cwd=workspace,
        workspace_root=workspace,
        environment=dict(os.environ),
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "canonical-ok"
    assert result.argv[0] == str(canonical_target)
    assert result.argv[0] != str(alias)


def test_opaque_executable_reparse_fails_before_launch(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = pathlib.Path(sys.executable).resolve(strict=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = workspace / "opaque-reparse.txt"
    payload = (
        "from pathlib import Path; "
        "Path('opaque-reparse.txt').write_text('bad', encoding='utf-8')"
    )

    monkeypatch.setattr(execution, "_is_windows_reparse_point", lambda _stat: True)

    with pytest.raises(ProcessExecutionError, match="reparse"):
        run_typed_process(
            (str(target), "-c", payload),
            process_policy=ProcessPolicy((str(target),)),
            resource_budget=_budget(),
            cwd=workspace,
            workspace_root=workspace,
            environment=dict(os.environ),
        )

    assert not marker.exists()


def test_allowlisted_executable_symlink_loop_fails_before_launch(
    tmp_path: pathlib.Path,
) -> None:
    first = tmp_path / "first-runner"
    second = tmp_path / "second-runner"
    _make_symlink(first, second)
    _make_symlink(second, first)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(ProcessExecutionError, match="loop"):
        run_typed_process(
            (str(first), "--version"),
            process_policy=ProcessPolicy((str(first),)),
            resource_budget=_budget(),
            cwd=workspace,
            workspace_root=workspace,
            environment=dict(os.environ),
        )
