from __future__ import annotations

import os

import pytest

from nika_core.toolsmith.workspace_security import WorkspaceSecurityError, validate_typed_argv


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable identity proof")
def test_posix_executable_identity_is_case_sensitive() -> None:
    with pytest.raises(WorkspaceSecurityError, match="exactly allowlisted"):
        validate_typed_argv(("/tmp/PYTHON", "-c", "pass"), {"/tmp/python"})


@pytest.mark.skipif(os.name != "nt", reason="Windows executable identity proof")
def test_windows_executable_identity_normalizes_case_and_separators() -> None:
    argv = (r"C:\Python\python.exe", "-c", "pass")
    assert validate_typed_argv(argv, {"c:/python/PYTHON.EXE"}) == argv
