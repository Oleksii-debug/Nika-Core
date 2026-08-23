from __future__ import annotations

import os
import pathlib
import sys

import pytest

from nika_core.toolsmith import ProcessPolicy, ResourceBudget
from nika_core.toolsmith.execution import run_typed_process
from nika_core.toolsmith.workspace_security import WorkspaceSecurityError


def test_process_runner_rejects_symlinked_workspace_root_before_launch(
    tmp_path: pathlib.Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = tmp_path / "workspace"
    try:
        workspace.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symbolic-link creation is unavailable on this runner")
    marker = outside / "must-not-exist.txt"
    code = (
        "import pathlib,sys; "
        "pathlib.Path(sys.argv[1]).write_text('launched', encoding='utf-8')"
    )

    with pytest.raises(WorkspaceSecurityError, match="workspace root"):
        run_typed_process(
            (sys.executable, "-c", code, str(marker)),
            process_policy=ProcessPolicy((sys.executable,)),
            resource_budget=ResourceBudget(
                timeout_seconds=5,
                max_output_bytes=4096,
                max_changed_files=1,
            ),
            cwd=workspace,
            workspace_root=workspace,
            environment=dict(os.environ),
        )

    assert not marker.exists(), "symlinked process workspace root escaped before launch"
