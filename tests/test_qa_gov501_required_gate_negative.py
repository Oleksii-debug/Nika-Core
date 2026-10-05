from __future__ import annotations

import os
import sys

import pytest


def test_qa_only_core_gate_fails_closed_when_windows_verify_fails() -> None:
    """Force one Core matrix leg red so the aggregate gate must also be red."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        pytest.skip("QA_ONLY oracle runs only under hosted GitHub Actions")
    if os.environ.get("GITHUB_WORKFLOW") != "Core CI":
        pytest.skip("QA_ONLY oracle targets only the Core CI workflow")

    assert sys.platform != "win32", (
        "QA_ONLY expected failure: Windows Core verify must be red so "
        "the dependent Core required gate can prove fail-closed behavior"
    )
