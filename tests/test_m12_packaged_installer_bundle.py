from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from nika_core.packaging.release import build_release_manifest, verify_release_manifest
from scripts.m11_release import _stage_canonical_installer

SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"


def test_canonical_installer_is_staged_and_manifest_bound(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    scripts = project_root / "scripts"
    scripts.mkdir(parents=True)
    canonical = scripts / "install_nika_core.ps1"
    canonical_bytes = b"Write-Output 'canonical installer'\n"
    canonical.write_bytes(canonical_bytes)

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "NikaCore.exe").write_bytes(b"binary")

    packaged = _stage_canonical_installer(project_root, bundle)

    assert packaged == bundle / "install_nika_core.ps1"
    assert packaged.read_bytes() == canonical_bytes

    manifest = build_release_manifest(
        bundle,
        product="NikaCore",
        version="0.0.2",
        source_sha=SOURCE_SHA,
    )
    installer_entry = next(
        entry for entry in manifest.files if entry.path == "install_nika_core.ps1"
    )
    assert installer_entry.size == len(canonical_bytes)
    assert installer_entry.sha256 == hashlib.sha256(canonical_bytes).hexdigest()
    assert verify_release_manifest(bundle, manifest) == ()


def test_staging_canonical_installer_fails_closed_when_source_is_missing(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    bundle = tmp_path / "bundle"
    bundle.mkdir()

    with pytest.raises(RuntimeError, match="canonical Windows installer is missing or unsafe"):
        _stage_canonical_installer(project_root, bundle)
