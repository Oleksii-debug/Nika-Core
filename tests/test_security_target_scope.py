from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.security import SandboxPolicy


def _sandbox(tmp_path: Path, **overrides: object) -> SandboxPolicy:
    values: dict[str, object] = {
        "workspace_root": tmp_path / "workspace",
        "writable_roots": ("artifacts", "worktrees"),
        "allowed_network_hosts": (),
        "allowed_executables": ("pytest", "python.exe"),
    }
    values.update(overrides)
    return SandboxPolicy(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "path",
    (
        r"C:\outside\secret.txt",
        r"C:outside\secret.txt",
        r"\outside\secret.txt",
        r"\\server\share\secret.txt",
        "/outside/secret.txt",
        r"worktrees\..\secret.txt",
    ),
)
def test_write_scope_rejects_windows_and_posix_escape_forms_cross_platform(
    tmp_path: Path, path: str
) -> None:
    sandbox = _sandbox(tmp_path)
    with pytest.raises(PermissionError, match="workspace-relative scope"):
        sandbox.resolve_write(path)


@pytest.mark.parametrize(
    "path",
    (
        "worktrees/repo/.git/config",
        "worktrees/repo/.GIT/hooks/pre-commit",
        "artifacts/report.txt:secret",
        "artifacts/report.",
        "artifacts/report ",
        "artifacts/NUL",
        "artifacts/NUL.txt",
        "artifacts/COM1.log",
        "artifacts/LPT².txt",
        "artifacts/bad?.txt",
    ),
)
def test_write_scope_rejects_windows_alias_metadata_and_device_targets(
    tmp_path: Path, path: str
) -> None:
    sandbox = _sandbox(tmp_path)
    with pytest.raises(PermissionError):
        sandbox.resolve_write(path)


def test_backslash_paths_are_canonicalized_to_workspace_children(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    result = sandbox.resolve_write(r"artifacts\reports\result.txt")
    assert result == (tmp_path / "workspace" / "artifacts" / "reports" / "result.txt").resolve()


@pytest.mark.parametrize(
    "root",
    (
        r"C:\outside",
        r"C:outside",
        r"\outside",
        r"\\server\share",
        "../outside",
        ".",
        "worktrees/.git",
        "artifacts/NUL",
    ),
)
def test_invalid_writable_roots_fail_at_policy_construction(tmp_path: Path, root: str) -> None:
    with pytest.raises(ValueError):
        _sandbox(tmp_path, writable_roots=(root,))


def test_bare_executable_name_preserves_explicit_basename_semantics(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path, allowed_executables=("pytest",))
    sandbox.authorize_executable("/usr/bin/pytest")
    sandbox.authorize_executable(r"C:\tools\PYTEST")
    with pytest.raises(PermissionError, match="process executable"):
        sandbox.authorize_executable("/usr/bin/python")


def test_posix_path_scoped_executable_is_exact_and_case_sensitive(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path, allowed_executables=("/opt/nika/bin/worker",))
    sandbox.authorize_executable("/opt/nika/bin/worker")
    with pytest.raises(PermissionError, match="process executable"):
        sandbox.authorize_executable("/tmp/worker")
    with pytest.raises(PermissionError, match="process executable"):
        sandbox.authorize_executable("/opt/nika/bin/WORKER")
    with pytest.raises(PermissionError, match="process executable"):
        sandbox.authorize_executable("worker")


def test_windows_path_scoped_executable_is_exact_but_case_insensitive(tmp_path: Path) -> None:
    sandbox = _sandbox(
        tmp_path,
        allowed_executables=(r"C:\Trusted\Python.exe",),
    )
    sandbox.authorize_executable(r"c:\trusted\PYTHON.EXE")
    with pytest.raises(PermissionError, match="process executable"):
        sandbox.authorize_executable(r"C:\Other\Python.exe")
    with pytest.raises(PermissionError, match="process executable"):
        sandbox.authorize_executable("python.exe")


@pytest.mark.parametrize(
    "executable",
    (
        "bin/python",
        r"bin\python.exe",
        "./python",
        r"C:python.exe",
        "NUL.exe",
        "python?.exe",
    ),
)
def test_ambiguous_or_reserved_executable_allowlist_entries_fail_closed(
    tmp_path: Path, executable: str
) -> None:
    with pytest.raises(ValueError):
        _sandbox(tmp_path, allowed_executables=(executable,))


def test_malformed_requested_executable_is_permission_failure(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path, allowed_executables=("python.exe",))
    with pytest.raises(PermissionError, match="process executable"):
        sandbox.authorize_executable(r"bin\python.exe")
    with pytest.raises(PermissionError, match="process executable"):
        sandbox.authorize_executable("NUL.exe")
