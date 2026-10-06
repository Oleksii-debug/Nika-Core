from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import threading
import time

import pytest

from nika_core.toolsmith import IsolationClass, ProcessPolicy, ResourceBudget
from nika_core.toolsmith.execution import run_typed_process
from nika_core.toolsmith.workspace_security import (
    WorkspacePathPolicy,
    WorkspaceSecurityError,
    collect_tree_evidence,
    ensure_path_policy,
    sterile_git_environment,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="physical Windows security proof")


def _create_directory_junction(link: pathlib.Path, target: pathlib.Path) -> None:
    result = subprocess.run(
        ("cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)),
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"unable to create physical test junction: {result.stderr or result.stdout}")


def test_guarded_path_rejects_physical_directory_junction_escape(
    tmp_path: pathlib.Path,
) -> None:
    workspace = tmp_path / "workspace"
    allowed = workspace / "src"
    outside = tmp_path / "outside"
    allowed.mkdir(parents=True)
    outside.mkdir()
    (outside / "secret.txt").write_text("outside\n", encoding="utf-8")
    junction = allowed / "escape"
    _create_directory_junction(junction, outside)

    policy = WorkspacePathPolicy(("src",))
    with pytest.raises(WorkspaceSecurityError, match="reparse points"):
        ensure_path_policy(workspace, "src/escape/secret.txt", policy, must_exist=True)


def test_tree_evidence_refuses_physical_directory_junction(
    tmp_path: pathlib.Path,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "inside.txt").write_text("inside\n", encoding="utf-8")
    (outside / "secret.txt").write_text("must-not-be-hashed\n", encoding="utf-8")
    _create_directory_junction(workspace / "escape", outside)

    with pytest.raises(WorkspaceSecurityError, match="reparse points"):
        collect_tree_evidence(workspace)


def test_windows_job_cancellation_kills_descendant_process_tree(
    tmp_path: pathlib.Path,
) -> None:
    marker = tmp_path / "descendant-survived.txt"
    child_code = (
        "import pathlib,sys,time; "
        "time.sleep(2); "
        "pathlib.Path(sys.argv[1]).write_text('survived', encoding='utf-8')"
    )
    parent_code = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}, sys.argv[1]]); "
        "time.sleep(30)"
    )
    cancellation = threading.Event()

    def cancel_after_spawn() -> None:
        time.sleep(0.5)
        cancellation.set()

    canceller = threading.Thread(target=cancel_after_spawn, daemon=True)
    canceller.start()
    result = run_typed_process(
        (sys.executable, "-c", parent_code, str(marker)),
        process_policy=ProcessPolicy((sys.executable,)),
        resource_budget=ResourceBudget(
            timeout_seconds=10,
            max_output_bytes=4096,
            max_changed_files=1,
        ),
        cwd=tmp_path,
        environment=sterile_git_environment({"PATH": os.environ.get("PATH", "")}),
        cancellation_event=cancellation,
    )
    canceller.join(timeout=2)
    time.sleep(2.5)

    assert result.cancelled
    assert result.returncode != 0
    assert result.isolation_class is IsolationClass.PROCESS_CONTAINED
    assert not marker.exists(), "descendant escaped the Windows kill-on-close Job Object"
