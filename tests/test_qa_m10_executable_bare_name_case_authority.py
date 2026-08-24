from pathlib import Path

import pytest

from nika_core.security import SandboxPolicy


def test_bare_executable_name_grant_preserves_case_sensitive_identity(
    tmp_path: Path,
) -> None:
    """A generic name grant must not authorize a distinct POSIX PATH entry by casefold."""
    sandbox = SandboxPolicy(
        workspace_root=tmp_path / "workspace",
        allowed_executables=("pytest",),
    )

    sandbox.authorize_executable("pytest")

    with pytest.raises(PermissionError, match="process executable"):
        sandbox.authorize_executable("PYTEST")
