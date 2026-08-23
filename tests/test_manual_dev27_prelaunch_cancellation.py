from __future__ import annotations

import os
import pathlib
import sys
import threading

from nika_core.toolsmith import ProcessPolicy, ResourceBudget
from nika_core.toolsmith.execution import run_typed_process


def test_preexisting_cancellation_never_launches_process(tmp_path: pathlib.Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = workspace / "must-not-exist.txt"
    cancellation = threading.Event()
    cancellation.set()
    code = (
        "import pathlib,sys; "
        "pathlib.Path(sys.argv[1]).write_text('launched', encoding='utf-8')"
    )

    result = run_typed_process(
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
        cancellation_event=cancellation,
    )

    assert result.cancelled
    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert not marker.exists(), "pre-cancelled worker executed a process side effect"
    assert not (workspace / "_nika_process_tmp").exists(), (
        "pre-cancelled worker performed process-environment setup"
    )
