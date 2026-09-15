from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from nika_core.packaging.release import build_release_manifest, write_release_manifest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install_nika_core.ps1"
SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"


def _bundle(root: Path, marker: str) -> Path:
    bundle = root / f"bundle-{marker}"
    bundle.mkdir(parents=True)
    (bundle / "NikaCore.exe").write_text(marker, encoding="utf-8")
    internal = bundle / "_internal"
    internal.mkdir()
    (internal / "runtime.dat").write_text(f"runtime-{marker}", encoding="utf-8")
    manifest = build_release_manifest(
        bundle,
        product="NikaCore",
        version=f"0.0.{marker[-1]}",
        source_sha=SOURCE_SHA,
    )
    write_release_manifest(bundle, manifest)
    return bundle


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def _run(
    shell: str,
    *,
    mode: str,
    destination: Path,
    bundle: Path | None = None,
    operation_id: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [
        shell,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(SCRIPT),
        "-Mode",
        mode,
        "-Destination",
        str(destination),
    ]
    if bundle is not None:
        command.extend(["-BundlePath", str(bundle)])
    if operation_id is not None:
        command.extend(["-RollbackOperationId", operation_id])
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
        timeout=30,
        env=os.environ.copy(),
    )


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_markerless_interrupted_rollback_swap_fails_before_recovery_mutation(
    tmp_path: Path,
) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    bundle_v1 = _bundle(tmp_path / "v1", "v1")
    bundle_v2 = _bundle(tmp_path / "v2", "v2")
    destination = tmp_path / "install" / "Nika Core"
    rollback = destination.parent / f".{destination.name}.rollback"
    swap = destination.parent / f".{destination.name}.rollback-swap"
    marker = destination.parent / f".{destination.name}.rollback-operation.json"

    installed = _run(
        shell,
        mode="Install",
        destination=destination,
        bundle=bundle_v1,
    )
    assert installed.returncode == 0, installed.stderr or installed.stdout
    updated = _run(
        shell,
        mode="Update",
        destination=destination,
        bundle=bundle_v2,
    )
    assert updated.returncode == 0, updated.stderr or updated.stdout
    assert not marker.exists()
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v2"
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v1"

    destination.rename(swap)
    assert not destination.exists()
    assert not marker.exists()

    rejected = _run(
        shell,
        mode="Rollback",
        destination=destination,
        operation_id="6" * 32,
    )
    assert rejected.returncode != 0
    assert "durable rollback operation marker" in (rejected.stderr + rejected.stdout).lower()
    assert not destination.exists()
    assert not marker.exists()
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert (swap / "NikaCore.exe").read_text(encoding="utf-8") == "v2"


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_markerless_interrupted_rollback_after_activation_fails_without_mutation(
    tmp_path: Path,
) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    bundle_v1 = _bundle(tmp_path / "v1", "v1")
    bundle_v2 = _bundle(tmp_path / "v2", "v2")
    destination = tmp_path / "install" / "Nika Core"
    rollback = destination.parent / f".{destination.name}.rollback"
    swap = destination.parent / f".{destination.name}.rollback-swap"
    marker = destination.parent / f".{destination.name}.rollback-operation.json"

    installed = _run(
        shell,
        mode="Install",
        destination=destination,
        bundle=bundle_v1,
    )
    assert installed.returncode == 0, installed.stderr or installed.stdout
    updated = _run(
        shell,
        mode="Update",
        destination=destination,
        bundle=bundle_v2,
    )
    assert updated.returncode == 0, updated.stderr or updated.stdout
    assert not marker.exists()
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v2"
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v1"

    destination.rename(swap)
    rollback.rename(destination)
    assert not rollback.exists()
    assert not marker.exists()
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert (swap / "NikaCore.exe").read_text(encoding="utf-8") == "v2"

    rejected = _run(
        shell,
        mode="Rollback",
        destination=destination,
        operation_id="7" * 32,
    )
    assert rejected.returncode != 0
    assert "durable rollback operation marker" in (rejected.stderr + rejected.stdout).lower()
    assert not marker.exists()
    assert not rollback.exists()
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert (swap / "NikaCore.exe").read_text(encoding="utf-8") == "v2"
