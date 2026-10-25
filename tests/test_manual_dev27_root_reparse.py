from __future__ import annotations

import pathlib

import pytest

from nika_core.toolsmith.workspace_security import (
    WorkspacePathPolicy,
    WorkspaceSecurityError,
    collect_tree_evidence,
    ensure_path_policy,
    sterile_process_environment,
)


def _directory_symlink(link: pathlib.Path, target: pathlib.Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symbolic-link creation is unavailable on this runner")


def test_tree_evidence_rejects_root_symlink_before_reading_external_tree(
    tmp_path: pathlib.Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("must-not-be-evidence\n", encoding="utf-8")
    root = tmp_path / "worker-root"
    _directory_symlink(root, outside)

    with pytest.raises(WorkspaceSecurityError, match="tree evidence root"):
        collect_tree_evidence(root)


def test_guarded_path_rejects_symlinked_workspace_root(tmp_path: pathlib.Path) -> None:
    outside = tmp_path / "outside"
    (outside / "src").mkdir(parents=True)
    root = tmp_path / "worker-root"
    _directory_symlink(root, outside)

    with pytest.raises(WorkspaceSecurityError, match="workspace root"):
        ensure_path_policy(
            root,
            "src/candidate.py",
            WorkspacePathPolicy(("src",)),
        )


def test_process_environment_rejects_symlinked_temp_root(tmp_path: pathlib.Path) -> None:
    outside = tmp_path / "outside-temp"
    outside.mkdir()
    temp_root = tmp_path / "worker-temp"
    _directory_symlink(temp_root, outside)

    with pytest.raises(WorkspaceSecurityError, match="worker temp root"):
        sterile_process_environment({"PATH": "trusted"}, temp_root=temp_root)
