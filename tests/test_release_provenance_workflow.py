from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = (
    ROOT / ".github/workflows/m11-windows-release.yml",
    ROOT / ".github/workflows/m12-prehuman-release-gate.yml",
)
ACTUAL_RUNTIME_INSTALL = (
    "python -m pip install --ignore-installed --quiet --constraint "
    "$constraints --report "
)


@pytest.mark.parametrize("workflow_path", WORKFLOWS, ids=lambda path: path.name)
def test_packaged_release_provenance_comes_from_actual_runtime_install(
    workflow_path: Path,
) -> None:
    workflow = workflow_path.read_text(encoding="utf-8")

    assert "pip install --dry-run" not in workflow
    assert ACTUAL_RUNTIME_INSTALL in workflow
    assert workflow.index(ACTUAL_RUNTIME_INSTALL) < workflow.index(
        "python scripts/m11_release.py"
    )
