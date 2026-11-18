"""Source drift must fail before hashed dependency installation in CI."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from scripts.plan1_dependency_lock import INPUTS, MANIFEST, OUTPUTS, verify_lock

ROOT = Path(__file__).resolve().parents[1]


def copy_lock(tmp_path: Path) -> Path:
    root = tmp_path / "Проєкт Олексія"
    for name in (*INPUTS, *OUTPUTS, MANIFEST):
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, destination)
    return root


def test_checked_in_lock_and_all_exported_profiles_match_declared_inputs() -> None:
    verify_lock(ROOT)


@pytest.mark.parametrize("name", [*INPUTS, *OUTPUTS])
def test_modified_source_or_resolved_profile_is_rejected_and_original_recovers(
    tmp_path: Path, name: str,
) -> None:
    root = copy_lock(tmp_path)
    path = root / name
    original = path.read_bytes()
    path.write_bytes(original + b"\n")
    with pytest.raises(ValueError, match="Stale or modified"):
        verify_lock(root)
    path.write_bytes(original)
    verify_lock(root)


def test_omitting_a_profile_cannot_hide_an_unverified_export(tmp_path: Path) -> None:
    root = copy_lock(tmp_path)
    path = root / MANIFEST
    manifest = json.loads(path.read_text(encoding="utf-8"))
    del manifest["sha256"]["requirements/m12-windows.txt"]
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="inventory differs"):
        verify_lock(root)


def test_core_and_release_jobs_use_the_locked_install_and_publish_observations() -> None:
    for filename in ("ci.yml", "m11-windows-release.yml", "m12-prehuman-release-gate.yml"):
        text = (ROOT / ".github" / "workflows" / filename).read_text(encoding="utf-8")
        assert "python scripts/plan1_dependency_lock.py" in text
        assert "--require-hashes -r requirements/build.txt" in text
        assert "--require-hashes --no-build-isolation -r requirements/" in text
        assert "--no-deps --no-build-isolation -e ." in text
        assert "--include-transitive --strict --output plan1-dependencies.json" in text
        assert "Preserve exact installed dependency and license-text evidence" in text
        # Actions checks LASTEXITCODE after each run step on Windows. A multiline
        # PowerShell step could overwrite a failed native command with a later
        # successful exit. Every admission/install/observation command is its
        # own step so a stale lock or failed pip hash stops the entire job.
        commands = (
            "python scripts/plan1_dependency_lock.py",
            "python -m pip install --require-hashes -r requirements/build.txt",
            "python -m pip install --require-hashes --no-build-isolation -r requirements/",
            "python -m pip install --no-deps --no-build-isolation -e .",
            "python scripts/plan1_dependency_environment.py --groups ",
        )
        for command in commands:
            assert text.count(f"run: {command}") == text.count(command)
        if filename != "ci.yml":
            assert text.count('      - "requirements/**"') == 2
            assert text.count('      - "uv.lock"') == 2
