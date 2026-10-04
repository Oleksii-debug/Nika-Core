from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from nika_core.packaging.windows import WindowsBuildPlan, default_windows_plan


def _plan(tmp_path: Path) -> WindowsBuildPlan:
    root = tmp_path / "worktree"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "nika_windows.py").write_text("pass\n", encoding="utf-8")
    web = root / "src" / "nika_core" / "ui" / "web"
    web.mkdir(parents=True)
    (web / "index.html").write_text("<main>Nika</main>", encoding="utf-8")
    (web / "app.js").write_text("console.log('Nika')", encoding="utf-8")
    (web / "styles.css").write_text("body {}", encoding="utf-8")
    return default_windows_plan(root)


def _symlink(path: Path, target: Path) -> None:
    try:
        path.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")


def test_plain_source_ancestors_remain_accepted(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    assert "--onedir" in plan.pyinstaller_args()


@pytest.mark.parametrize("source", ("entrypoint", "web_assets"))
def test_linked_source_ancestor_cannot_import_outside_tree(
    tmp_path: Path, source: str
) -> None:
    plan = _plan(tmp_path)
    root = tmp_path / "worktree"
    ancestor = root / ("scripts" if source == "entrypoint" else "src" / Path("nika_core"))
    # Source assets and entrypoint remain ordinary files, so checking only
    # their immediate paths cannot detect the redirected source directory.
    outside = tmp_path / ("Зовнішні файли" if source == "entrypoint" else "outside-ui")
    ancestor.rename(outside)
    _symlink(ancestor, outside)
    assert not plan.entrypoint.is_symlink()
    assert not plan.web_assets.is_symlink()
    with pytest.raises(ValueError, match="path traverses a symbolic link or junction"):
        plan.pyinstaller_args()


def test_linked_shared_workspace_ancestor_cannot_redirect_both_inputs(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    root = tmp_path / "worktree"
    outside = tmp_path / "original-worktree"
    root.rename(outside)
    _symlink(root, outside)
    assert not plan.entrypoint.is_symlink()
    assert not plan.web_assets.is_symlink()
    with pytest.raises(ValueError, match="path traverses a symbolic link or junction"):
        plan.pyinstaller_args()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction-only regression")
def test_windows_source_parent_junction_is_rejected(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    parent = tmp_path / "worktree" / "scripts"
    external = tmp_path / "external-scripts"
    parent.rename(external)
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(parent), str(external)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        pytest.skip("Windows junction creation unavailable on this runner")
    assert parent.is_junction()
    assert not plan.entrypoint.is_junction()
    with pytest.raises(ValueError, match="path traverses a symbolic link or junction"):
        plan.pyinstaller_args()
