from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import nika_core.product_project as product_project_module
from nika_core.data.sqlite import SQLiteStore
from nika_core.packaging.release import build_release_manifest, verify_release_manifest
from nika_core.product_project import ProductProjectRepository, ProductProjectSpec
from scripts.m11_release import _stage_canonical_installer
from scripts.m12_release_evidence import (
    _read_durable_project_witness,
    _require_durable_project_continuity,
)

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


def _create_continuity_project(data_path: Path) -> None:
    store = SQLiteStore(data_path)
    store.initialize()
    ProductProjectRepository(store).create(
        project_id="product-project-deterministic",
        name="Packaged acceptance project",
        spec=ProductProjectSpec(
            goal="Create a durable packaged acceptance project",
            desired_outcome="ProductProject survives installer image transitions",
        ),
        idempotency_key="packaged-acceptance-project",
    )


def test_durable_project_continuity_rejects_recreated_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_path = tmp_path / "durable-data" / "nika_core.db"
    proof: dict[str, object] = {
        "route": "product_project",
        "project_id": "product-project-deterministic",
        "spec_version": 1,
    }

    monkeypatch.setattr(
        product_project_module,
        "_now",
        lambda: "2026-09-14T12:00:00+00:00",
    )
    _create_continuity_project(data_path)
    install_witness = _read_durable_project_witness(data_path, proof)
    assert (
        _require_durable_project_continuity(
            install_witness,
            data_path,
            proof,
            phase="Update",
        )
        == install_witness
    )

    data_path.unlink()
    monkeypatch.setattr(
        product_project_module,
        "_now",
        lambda: "2026-09-14T12:00:01+00:00",
    )
    _create_continuity_project(data_path)
    recreated_witness = _read_durable_project_witness(data_path, proof)

    assert recreated_witness[:3] == install_witness[:3]
    assert recreated_witness[3] != install_witness[3]
    with pytest.raises(
        RuntimeError,
        match="Update did not preserve the durable ProductProject row",
    ):
        _require_durable_project_continuity(
            install_witness,
            data_path,
            proof,
            phase="Update",
        )
