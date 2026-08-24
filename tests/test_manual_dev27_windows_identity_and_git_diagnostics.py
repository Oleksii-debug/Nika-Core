from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

from nika_core.toolsmith import execution, workspace_security


_GIT_SECRET_CANARY = "DEV27_SYNTHETIC_GIT_SECRET_8bca0d7f"


@pytest.mark.skipif(os.name != "nt", reason="Windows executable identity proof")
@pytest.mark.parametrize(
    ("requested", "allowlisted"),
    (
        (r"\\?\C:\Trusted\python.exe", r"C:\Trusted\python.exe"),
        (r"C:\Trusted\python.exe", r"\\?\c:\trusted\PYTHON.EXE"),
    ),
)
def test_windows_extended_local_drive_identity_matches_ordinary_path(
    requested: str,
    allowlisted: str,
) -> None:
    argv = (requested, "-c", "pass")
    assert workspace_security.validate_typed_argv(argv, (allowlisted,)) == argv


@pytest.mark.skipif(os.name != "nt", reason="Windows executable identity proof")
@pytest.mark.parametrize(
    ("requested", "allowlisted"),
    (
        (r"\\?\UNC\server\share\python.exe", r"\\server\share\python.exe"),
        (r"\\.\C:\Trusted\python.exe", r"C:\Trusted\python.exe"),
        (
            r"\\?\GLOBALROOT\Device\HarddiskVolume1\Trusted\python.exe",
            r"C:\Trusted\python.exe",
        ),
    ),
)
def test_windows_device_namespaces_do_not_gain_local_drive_identity(
    requested: str,
    allowlisted: str,
) -> None:
    with pytest.raises(workspace_security.WorkspaceSecurityError, match="exactly allowlisted"):
        workspace_security.validate_typed_argv((requested, "-c", "pass"), (allowlisted,))


@pytest.mark.skipif(os.name != "nt", reason="Windows executable identity proof")
def test_windows_extended_local_drive_identity_does_not_bypass_shell_denial() -> None:
    requested = r"\\?\C:\Windows\System32\cmd.exe"
    allowlisted = r"C:\Windows\System32\cmd.exe"
    with pytest.raises(workspace_security.WorkspaceSecurityError, match="generic shell"):
        workspace_security.validate_typed_argv(
            (requested, "/c", "whoami"),
            (allowlisted,),
        )


def test_git_failure_diagnostic_does_not_expose_raw_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    stderr = (
        "fatal: unable to access "
        f"'https://dev27-user:{_GIT_SECRET_CANARY}@example.invalid/private.git/': denied\n"
    )
    stdout = f"Authorization: Bearer {_GIT_SECRET_CANARY}\n"

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        return subprocess.CompletedProcess(
            args=("git", "fetch"),
            returncode=128,
            stdout=stdout,
            stderr=stderr,
        )

    monkeypatch.setattr(execution.subprocess, "run", fake_run)

    with pytest.raises(workspace_security.WorkspaceSecurityError) as exc_info:
        execution._git(
            ("git", "fetch"),
            cwd=tmp_path,
            environment={},
        )

    escaped = f"{exc_info.value!s}\n{exc_info.value!r}"
    assert _GIT_SECRET_CANARY not in escaped
    assert "dev27-user:" not in escaped
    assert str(exc_info.value) == "git command failed (exit 128)"


def test_git_timeout_diagnostic_does_not_expose_argv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    secret_url = f"https://dev27-user:{_GIT_SECRET_CANARY}@example.invalid/private.git"

    def fake_timeout(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        raise subprocess.TimeoutExpired(("git", "fetch", secret_url), timeout=60)

    monkeypatch.setattr(execution.subprocess, "run", fake_timeout)

    with pytest.raises(
        workspace_security.WorkspaceSecurityError,
        match="git command timed out",
    ) as exc_info:
        execution._git(
            ("git", "fetch", secret_url),
            cwd=tmp_path,
            environment={},
        )

    escaped = f"{exc_info.value!s}\n{exc_info.value!r}"
    assert _GIT_SECRET_CANARY not in escaped
    assert "dev27-user:" not in escaped
