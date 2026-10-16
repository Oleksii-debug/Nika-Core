from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.workspaces import WorkspaceResolver


@pytest.mark.parametrize(
    "untrusted",
    [
        "/tmp/outside.txt",
        r"C:\Windows\win.ini",
        r"C:drive-relative.txt",
        r"\Windows\win.ini",
        r"\\server\share\outside.txt",
        r"\\?\C:\Windows\win.ini",
        r"subdir\..\..\outside.txt",
        "subdir/../report.txt",
        "report.txt:alternate-stream",
        r"subdir\report.txt:alternate-stream",
    ],
)
def test_workspace_resolver_rejects_cross_platform_escape_or_stream(
    tmp_path: Path, untrusted: str
) -> None:
    resolver = WorkspaceResolver(tmp_path / "workspace")
    with pytest.raises(ValueError, match="workspace path"):
        resolver.resolve(untrusted)


@pytest.mark.parametrize("untrusted", ["", "\x00", "subdir/\x00file", None, 123])
def test_workspace_resolver_rejects_invalid_path_text(
    tmp_path: Path, untrusted: object
) -> None:
    resolver = WorkspaceResolver(tmp_path / "workspace")
    with pytest.raises(ValueError, match="nonempty text without NUL"):
        resolver.resolve(untrusted)  # type: ignore[arg-type]


def test_workspace_resolver_retains_unicode_and_nested_relative_paths(tmp_path: Path) -> None:
    resolver = WorkspaceResolver(tmp_path / "робоча папка")
    expected = (tmp_path / "робоча папка" / "документи" / "звіт 1.txt").resolve()

    assert resolver.resolve("документи/звіт 1.txt") == expected
    assert resolver.resolve(r"документи\звіт 1.txt") == expected
    assert resolver.resolve(".") == resolver.root


def test_workspace_resolver_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this host")
    resolver = WorkspaceResolver(root)
    with pytest.raises(ValueError, match="escapes configured root"):
        resolver.resolve("linked/data.txt")
