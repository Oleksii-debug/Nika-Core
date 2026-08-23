from __future__ import annotations

import pathlib
import re

import pytest

_WORKFLOWS = (
    ".github/workflows/ci.yml",
    ".github/workflows/m11-windows-release.yml",
    ".github/workflows/m12-prehuman-release-gate.yml",
)
_ACTION_USE = re.compile(r"^\s*-?\s*uses:\s*([^\s#]+)", re.MULTILINE)
_FULL_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")


@pytest.mark.parametrize("workflow_path", _WORKFLOWS)
def test_release_critical_actions_are_pinned_to_immutable_commits(workflow_path: str) -> None:
    """Release evidence must not execute mutable third-party action tags."""
    text = pathlib.Path(workflow_path).read_text(encoding="utf-8")
    mutable: list[str] = []

    for action_ref in _ACTION_USE.findall(text):
        if action_ref.startswith("./"):
            continue
        action, separator, ref = action_ref.rpartition("@")
        if not separator or not action or not _FULL_COMMIT_SHA.fullmatch(ref):
            mutable.append(action_ref)

    assert not mutable, (
        f"{workflow_path} contains mutable action references: {mutable}. "
        "Pin every third-party action to a full 40-character commit SHA before "
        "using its output for CI or release/provenance evidence."
    )
