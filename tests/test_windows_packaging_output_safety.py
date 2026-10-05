from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from nika_core.packaging.windows import WindowsBuildPlan, default_windows_plan


def _plan(tmp_path: Path) -> WindowsBuildPlan:
    entrypoint = tmp_path / "nika_windows.py"
    entrypoint.write_text("pass\n", encoding="utf-8")
    assets = tmp_path / "web"
    assets.mkdir()
    (assets / "index.html").write_text("<main>Nika</main>", encoding="utf-8")
    (assets / "app.js").write_text("console.log('Nika')", encoding="utf-8")
    (assets / "styles.css").write_text("body {}", encoding="utf-8")
    return WindowsBuildPlan(
        entrypoint=entrypoint,
        web_assets=assets,
        dist_dir=tmp_path / "dist",
        work_dir=tmp_path / "build" / "work",
        spec_dir=tmp_path / "build" / "spec",
    )


def _link_directory(path: Path, target: Path) -> None:
    try:
        path.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")


def test_plain_release_output_paths_remain_accepted(tmp_path: Path) -> None:
    plan = _plan(tmp_path)

    assert "--onedir" in plan.pyinstaller_args()
    assert plan.bundle_dir == tmp_path / "dist" / "NikaCore"


@pytest.mark.parametrize("field", ("dist_dir", "work_dir", "spec_dir"))
def test_linked_release_output_directory_is_rejected(
    tmp_path: Path,
    field: str,
) -> None:
    plan = _plan(tmp_path)
    target = tmp_path / f"external-{field}"
    target.mkdir()
    alias = tmp_path / f"linked-{field}"
    _link_directory(alias, target)
    linked = replace(plan, **{field: alias})

    with pytest.raises(ValueError, match=f"{field} path traverses"):
        linked.pyinstaller_args()
    if field == "dist_dir":
        with pytest.raises(ValueError, match="dist_dir path traverses"):
            _ = linked.bundle_dir


def test_linked_release_output_ancestor_is_rejected(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    external = tmp_path / "external-build"
    external.mkdir()
    alias = tmp_path / "linked-build"
    _link_directory(alias, external)
    linked = replace(
        plan,
        work_dir=alias / "work",
        spec_dir=alias / "spec",
    )

    with pytest.raises(ValueError, match="work_dir path traverses"):
        linked.pyinstaller_args()


def test_broken_linked_release_output_ancestor_is_rejected(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    alias = tmp_path / "broken-output"
    _link_directory(alias, tmp_path / "missing-output")
    linked = replace(plan, dist_dir=alias / "dist")

    with pytest.raises(ValueError, match="dist_dir path traverses"):
        linked.pyinstaller_args()
    with pytest.raises(ValueError, match="dist_dir path traverses"):
        _ = linked.bundle_dir


def test_existing_linked_bundle_leaf_is_rejected_before_packaging(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    plan.dist_dir.mkdir()
    external = tmp_path / "external-bundle"
    external.mkdir()
    _link_directory(plan.dist_dir / plan.name, external)

    with pytest.raises(ValueError, match="bundle_dir path traverses"):
        plan.pyinstaller_args()


def test_bundle_leaf_is_revalidated_after_build_boundary(tmp_path: Path) -> None:
    plan = _plan(tmp_path)

    # Initial source/output authority is valid while the eventual bundle leaf
    # does not exist.
    assert "--onedir" in plan.pyinstaller_args()

    plan.dist_dir.mkdir()
    external = tmp_path / "post-build-external"
    external.mkdir()
    _link_directory(plan.dist_dir / plan.name, external)

    with pytest.raises(ValueError, match="bundle_dir path traverses"):
        _ = plan.bundle_dir


def test_explicit_linked_project_root_is_rejected_before_resolve(tmp_path: Path) -> None:
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    alias = tmp_path / "linked-root"
    _link_directory(alias, real_root)

    with pytest.raises(ValueError, match="project_root path traverses"):
        default_windows_plan(alias)
