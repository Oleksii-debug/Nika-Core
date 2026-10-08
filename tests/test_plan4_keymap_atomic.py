"""Keyboard/NVDA-first Plan 4 keymap projection regression."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_plan4_atomic_keymap_readback() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js not installed")
    run = subprocess.run(
        [
            node,
            str(ROOT / "tests/js/plan4_keymap_atomic_harness.cjs"),
            str(ROOT / "src/nika_core/ui/web/app.js"),
        ],
        cwd=ROOT, capture_output=True, text=True, timeout=15, check=False,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.count("PASS:") == 4, run.stdout
