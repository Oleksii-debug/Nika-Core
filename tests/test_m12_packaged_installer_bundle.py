from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

import scripts.m11_release as m11_release
import nika_core.product_project as product_project_module
from nika_core.data.sqlite import SQLiteStore
from nika_core.packaging.release import build_release_manifest, verify_release_manifest
from nika_core.product_project import ProductProjectRepository, ProductProjectSpec
from scripts.m11_release import _stage_canonical_installer
from scripts.m12_release_evidence import (
    _build_fault_injected_rollback_installer,
    _installer_command,
    _read_durable_project_witness,
    _require_durable_project_continuity,
    _require_rollback_operation_marker,
    _rollback_operation_id,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"


def test_m11_runs_for_packaged_installer_and_binding_regression_changes() -> None:
    workflow = (ROOT / ".github/workflows/m11-windows-release.yml").read_text(
        encoding="utf-8"
    )
    required_triggers = (
        '      - "scripts/install_nika_core.ps1"',
        '      - "tests/test_m12_packaged_installer_bundle.py"',
    )
    pull_request_block, remainder = workflow.split("  push:\n", 1)
    push_block, _ = remainder.split("  workflow_dispatch:\n", 1)

    for trigger in required_triggers:
        assert workflow.count(trigger) == 2
        assert trigger in pull_request_block
        assert trigger in push_block


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


def test_staging_rejects_source_changed_between_lstat_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    scripts = project_root / "scripts"
    scripts.mkdir(parents=True)
    canonical = scripts / "install_nika_core.ps1"
    canonical.write_bytes(b"original installer\n")
    bundle = tmp_path / "bundle"
    bundle.mkdir()

    real_open = m11_release._open_readonly_nofollow_snapshot
    swapped = False

    def swapping_open(path: Path) -> int:
        nonlocal swapped
        if path == canonical and not swapped:
            swapped = True
            canonical.write_bytes(b"changed installer bytes\n")
        return real_open(path)

    monkeypatch.setattr(m11_release, "_open_readonly_nofollow_snapshot", swapping_open)
    with pytest.raises(RuntimeError, match="changed during staging"):
        _stage_canonical_installer(project_root, bundle)
    assert not (bundle / "install_nika_core.ps1").exists()
    assert not list(bundle.glob(".install_nika_core-*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="Windows file-share semantics")
def test_installer_staging_refuses_preexisting_writer_and_recovers_after_close(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    scripts = project_root / "scripts"
    scripts.mkdir(parents=True)
    canonical = scripts / "install_nika_core.ps1"
    canonical_bytes = b"Write-Output 'locked canonical installer'\n"
    canonical.write_bytes(canonical_bytes)
    bundle = tmp_path / "bundle"
    bundle.mkdir()

    with canonical.open("r+b"):
        with pytest.raises(RuntimeError, match="could not be staged safely"):
            _stage_canonical_installer(project_root, bundle)

    assert not (bundle / "install_nika_core.ps1").exists()
    assert not list(bundle.glob(".install_nika_core-*.tmp"))

    packaged = _stage_canonical_installer(project_root, bundle)
    assert packaged.read_bytes() == canonical_bytes


@pytest.mark.skipif(os.name != "nt", reason="Windows file-share semantics")
def test_installer_source_snapshot_holds_write_delete_fence_until_close(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "install_nika_core.ps1"
    renamed = tmp_path / "renamed-installer.ps1"
    canonical.write_bytes(b"canonical installer\n")

    descriptor = m11_release._open_readonly_nofollow_snapshot(canonical)
    try:
        assert os.read(descriptor, len(b"canonical installer\n")) == b"canonical installer\n"
        with pytest.raises(OSError):
            canonical.write_bytes(b"replacement\n")
        with pytest.raises(OSError):
            canonical.replace(renamed)
    finally:
        os.close(descriptor)

    canonical.replace(renamed)
    renamed.replace(canonical)
    canonical.write_bytes(b"replacement\n")
    assert canonical.read_bytes() == b"replacement\n"


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


def test_rollback_operation_id_is_stable_direction_bound_and_exact() -> None:
    source = "a" * 64
    target = "b" * 64

    first = _rollback_operation_id(source, target, label="crash-retry")
    repeated = _rollback_operation_id(source, target, label="crash-retry")
    reversed_id = _rollback_operation_id(target, source, label="distinct-reverse")

    assert first == repeated
    assert len(first) == 32
    assert all(character in "0123456789abcdef" for character in first)
    assert reversed_id != first


def test_installer_command_binds_explicit_rollback_operation_id() -> None:
    operation_id = "a" * 32
    command = _installer_command(
        "pwsh",
        Path("install_nika_core.ps1"),
        mode="Rollback",
        destination=Path("C:/NikaCore"),
        rollback_operation_id=operation_id,
    )

    assert command[-2:] == ["-RollbackOperationId", operation_id]

    with pytest.raises(ValueError, match="only valid for Rollback"):
        _installer_command(
            "pwsh",
            Path("install_nika_core.ps1"),
            mode="Update",
            destination=Path("C:/NikaCore"),
            rollback_operation_id=operation_id,
        )


def test_fault_injected_rollback_installer_adds_only_post_swap_failfast(
    tmp_path: Path,
) -> None:
    source = ROOT / "scripts" / "install_nika_core.ps1"
    target = tmp_path / "fault-injected.ps1"

    _build_fault_injected_rollback_installer(source, target)

    source_lines = source.read_text(encoding="utf-8-sig").splitlines()
    injected_lines = target.read_text(encoding="utf-8").splitlines()
    failfast = (
        '        [System.Environment]::FailFast('
        '"M12 injected crash after rollback final swap")'
    )
    assert injected_lines.count(failfast) == 1
    injected_lines.remove(failfast)
    assert injected_lines == source_lines

    final_swap = "        [System.IO.Directory]::Move($swapPath, $rollbackPath)"
    crash_lines = target.read_text(encoding="utf-8").splitlines()
    crash_index = crash_lines.index(failfast)
    assert injected_lines.count(final_swap) == 1
    assert crash_lines[crash_index - 1] == final_swap


def test_rollback_marker_evidence_requires_exact_operation_and_image_pair(
    tmp_path: Path,
) -> None:
    marker = tmp_path / ".Nika Core.rollback-operation.json"
    operation_id = "c" * 32
    source_digest = "a" * 64
    target_digest = "b" * 64
    marker.write_text(
        '{"marker_version":1,"operation_id":"'
        + operation_id
        + '","source_digest":"'
        + source_digest
        + '","target_digest":"'
        + target_digest
        + '"}',
        encoding="utf-8",
    )

    _require_rollback_operation_marker(
        marker,
        operation_id=operation_id,
        source_digest=source_digest,
        target_digest=target_digest,
    )

    with pytest.raises(RuntimeError, match="does not match exact image authority"):
        _require_rollback_operation_marker(
            marker,
            operation_id="d" * 32,
            source_digest=source_digest,
            target_digest=target_digest,
        )
