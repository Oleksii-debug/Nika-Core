from __future__ import annotations

import subprocess

import pytest

from nika_core.toolsmith import execution
from nika_core.toolsmith.workspace_security import WorkspaceSecurityError


_CANARY = "QA53_SYNTHETIC_GIT_URL_SECRET_4d93f1b7"


def test_git_failure_diagnostic_cannot_expose_credential_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """QA_ONLY oracle for DEV27 raw Git diagnostic propagation.

    Git may echo a credential-bearing remote URL in stderr. The low-level worker
    must not place that raw diagnostic into its public exception message/repr.
    This uses only a synthetic canary and does not invoke a real Git process.
    """

    stderr = (
        "fatal: unable to access "
        f"'https://qa53-user:{_CANARY}@example.invalid/private.git/': denied\n"
    )

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        return subprocess.CompletedProcess(
            args=("git", "fetch"),
            returncode=128,
            stdout="",
            stderr=stderr,
        )

    monkeypatch.setattr(execution.subprocess, "run", fake_run)

    with pytest.raises(WorkspaceSecurityError) as exc_info:
        execution._git(  # noqa: SLF001 - intentional QA oracle of exact boundary
            ("git", "fetch"),
            cwd=tmp_path,
            environment={},
        )

    escaped = f"{exc_info.value!s}\n{exc_info.value!r}"
    assert _CANARY not in escaped
    assert "qa53-user:" not in escaped
