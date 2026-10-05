from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
NODE = shutil.which("node")
APP = ROOT / "src" / "nika_core" / "ui" / "web" / "app.js"
HARNESS = ROOT / "tests" / "js" / "command_dispatch_singleflight_harness.cjs"


@pytest.mark.skipif(
    NODE is None,
    reason="Node.js is required for the Windows UI command regression",
)
def test_windows_ui_command_singleflight_and_uncertain_effects() -> None:
    for path in (APP, HARNESS):
        syntax = subprocess.run(
            [NODE, "--check", str(path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert syntax.returncode == 0, syntax.stderr

    result = subprocess.run(
        [NODE, str(HARNESS), str(APP)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("PASS:") == 21, result.stdout
