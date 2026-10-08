from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
NODE = shutil.which("node")
APP = ROOT / "src" / "nika_core" / "ui" / "web" / "app.js"
HARNESS = ROOT / "tests" / "js" / "plan4_state_read_order_harness.cjs"


@pytest.mark.skipif(NODE is None, reason="Node.js required for UI state race regression")
def test_plan4_accessible_state_reconciliation_order() -> None:
    for path in (APP, HARNESS):
        check = subprocess.run(
            [NODE, "--check", str(path)],
            cwd=ROOT, capture_output=True, text=True, timeout=15, check=False,
        )
        assert check.returncode == 0, check.stderr
    run = subprocess.run(
        [NODE, str(HARNESS), str(APP)],
        cwd=ROOT, capture_output=True, text=True, timeout=15, check=False,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.count("PASS:") == 19, run.stdout
