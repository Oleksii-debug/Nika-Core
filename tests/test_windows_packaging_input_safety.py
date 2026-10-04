from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.packaging.windows import WindowsBuildPlan


def _plan(tmp_path: Path) -> WindowsBuildPlan:
    entrypoint = tmp_path / "nika_windows.py"
    entrypoint.write_text("pass\n", encoding="utf-8")
    assets = tmp_path / "web"
    assets.mkdir()
    (assets / "index.html").write_text("<main>Nika</main>", encoding="utf-8")
    (assets / "app.js").write_text("console.log('Nika')", encoding="utf-8")
    (assets / "styles.css").write_text("body {}", encoding="utf-8")
    return WindowsBuildPlan(
        entrypoint, assets, tmp_path / "dist", tmp_path / "work", tmp_path / "spec"
    )


def _link(path: Path, target: Path, *, directory: bool = False) -> None:
    try:
        path.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")


def test_normal_website_is_accepted(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    assert "--onedir" in plan.pyinstaller_args()


def test_missing_entrypoint_is_rejected(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    plan.entrypoint.unlink()
    with pytest.raises(FileNotFoundError, match="entrypoint"):
        plan.validate()


@pytest.mark.parametrize("required", ("index.html", "app.js", "styles.css"))
def test_missing_or_empty_ui_file_fails_before_packaging(
    tmp_path: Path, required: str
) -> None:
    plan = _plan(tmp_path)
    asset = plan.web_assets / required
    asset.unlink()
    with pytest.raises(ValueError, match=f"non-empty {required}"):
        plan.pyinstaller_args()
    asset.write_bytes(b"")
    with pytest.raises(ValueError, match=f"non-empty {required}"):
        plan.pyinstaller_args()


def test_linked_entrypoint_is_rejected(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    alias = tmp_path / "entry-alias.py"
    _link(alias, plan.entrypoint)
    linked = WindowsBuildPlan(
        alias, plan.web_assets, plan.dist_dir, plan.work_dir, plan.spec_dir
    )
    with pytest.raises(ValueError, match="symbolic link or junction"):
        linked.pyinstaller_args()


def test_linked_web_root_is_rejected(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    alias = tmp_path / "linked-web"
    _link(alias, plan.web_assets, directory=True)
    linked = WindowsBuildPlan(
        plan.entrypoint, alias, plan.dist_dir, plan.work_dir, plan.spec_dir
    )
    with pytest.raises(ValueError, match="symbolic link or junction"):
        linked.pyinstaller_args()


@pytest.mark.parametrize("directory", (False, True))
def test_nested_link_cannot_import_external_asset(tmp_path: Path, directory: bool) -> None:
    plan = _plan(tmp_path)
    external = tmp_path / ("external" if directory else "private.txt")
    if directory:
        external.mkdir()
        (external / "secret.txt").write_text("private", encoding="utf-8")
    else:
        external.write_text("private", encoding="utf-8")
    alias = plan.web_assets / "external"
    _link(alias, external, directory=directory)
    with pytest.raises(ValueError, match="web_assets contains a symbolic link or junction"):
        plan.pyinstaller_args()


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows junction-only regression")
def test_nested_windows_junction_is_rejected_before_traversal(tmp_path: Path) -> None:
    import subprocess

    plan = _plan(tmp_path)
    outside = tmp_path / "Зовнішні файли"
    outside.mkdir()
    (outside / "private.txt").write_text("not for release", encoding="utf-8")
    link = plan.web_assets / "junction"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        pytest.skip("Windows junction creation is not supported by this runner")
    assert link.is_junction()
    with pytest.raises(ValueError, match="web_assets contains a symbolic link or junction"):
        plan.pyinstaller_args()
