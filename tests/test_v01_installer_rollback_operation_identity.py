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
    script: Path,
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
        str(script),
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


def test_rollback_operation_identity_contract_is_present() -> None:
    payload = SCRIPT.read_text(encoding="utf-8")
    assert '[string]$RollbackOperationId = ""' in payload
    assert ".$leaf.rollback-operation.json" in payload
    assert "operation_id" in payload
    assert "source_digest" in payload
    assert "target_digest" in payload


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_terminal_rollback_crash_same_operation_is_idempotent_and_new_operation_reverses(
    tmp_path: Path,
) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    bundle_v1 = _bundle(tmp_path / "v1", "v1")
    bundle_v2 = _bundle(tmp_path / "v2", "v2")
    destination = tmp_path / "install" / "Nika Core"
    rollback = destination.parent / f".{destination.name}.rollback"
    marker = destination.parent / f".{destination.name}.rollback-operation.json"

    installed = _run(
        shell,
        script=SCRIPT,
        mode="Install",
        destination=destination,
        bundle=bundle_v1,
    )
    assert installed.returncode == 0, installed.stderr or installed.stdout
    updated = _run(
        shell,
        script=SCRIPT,
        mode="Update",
        destination=destination,
        bundle=bundle_v2,
    )
    assert updated.returncode == 0, updated.stderr or updated.stdout

    payload = SCRIPT.read_text(encoding="utf-8")
    needle = (
        "        [System.IO.Directory]::Move($swapPath, $rollbackPath)\n"
        "        $rollbackPhase = \"complete\"\n"
    )
    assert payload.count(needle) == 1
    instrumented = tmp_path / "rollback-final-ack-crash.ps1"
    instrumented.write_text(
        payload.replace(
            needle,
            "        [System.IO.Directory]::Move($swapPath, $rollbackPath)\n"
            "        exit 91\n"
            "        $rollbackPhase = \"complete\"\n",
            1,
        ),
        encoding="utf-8",
    )

    first_operation = "1" * 32
    crashed = _run(
        shell,
        script=instrumented,
        mode="Rollback",
        destination=destination,
        operation_id=first_operation,
    )
    assert crashed.returncode == 91, crashed.stderr or crashed.stdout
    assert marker.is_file()
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v2"

    retried = _run(
        shell,
        script=SCRIPT,
        mode="Rollback",
        destination=destination,
        operation_id=first_operation,
    )
    assert retried.returncode == 0, retried.stderr or retried.stdout
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v2"

    second_operation = "2" * 32
    reversed_again = _run(
        shell,
        script=SCRIPT,
        mode="Rollback",
        destination=destination,
        operation_id=second_operation,
    )
    assert reversed_again.returncode == 0, reversed_again.stderr or reversed_again.stdout
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v2"
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v1"


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_same_rollback_operation_rejects_verified_pair_substitution_without_mutation(
    tmp_path: Path,
) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    bundle_v1 = _bundle(tmp_path / "v1", "v1")
    bundle_v2 = _bundle(tmp_path / "v2", "v2")
    bundle_v3 = _bundle(tmp_path / "v3", "v3")
    destination = tmp_path / "install" / "Nika Core"
    rollback = destination.parent / f".{destination.name}.rollback"
    operation_id = "3" * 32

    assert _run(
        shell,
        script=SCRIPT,
        mode="Install",
        destination=destination,
        bundle=bundle_v1,
    ).returncode == 0
    assert _run(
        shell,
        script=SCRIPT,
        mode="Update",
        destination=destination,
        bundle=bundle_v2,
    ).returncode == 0
    completed = _run(
        shell,
        script=SCRIPT,
        mode="Rollback",
        destination=destination,
        operation_id=operation_id,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v2"

    shutil.rmtree(rollback)
    shutil.copytree(bundle_v3, rollback)
    before_destination = (destination / "NikaCore.exe").read_bytes()
    before_rollback = (rollback / "NikaCore.exe").read_bytes()

    rejected = _run(
        shell,
        script=SCRIPT,
        mode="Rollback",
        destination=destination,
        operation_id=operation_id,
    )
    assert rejected.returncode != 0
    assert (destination / "NikaCore.exe").read_bytes() == before_destination
    assert (rollback / "NikaCore.exe").read_bytes() == before_rollback


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_malformed_rollback_operation_identity_fails_before_mutation(tmp_path: Path) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    bundle_v1 = _bundle(tmp_path / "v1", "v1")
    bundle_v2 = _bundle(tmp_path / "v2", "v2")
    destination = tmp_path / "install" / "Nika Core"
    rollback = destination.parent / f".{destination.name}.rollback"

    assert _run(
        shell,
        script=SCRIPT,
        mode="Install",
        destination=destination,
        bundle=bundle_v1,
    ).returncode == 0
    assert _run(
        shell,
        script=SCRIPT,
        mode="Update",
        destination=destination,
        bundle=bundle_v2,
    ).returncode == 0

    before_destination = (destination / "NikaCore.exe").read_bytes()
    before_rollback = (rollback / "NikaCore.exe").read_bytes()
    rejected = _run(
        shell,
        script=SCRIPT,
        mode="Rollback",
        destination=destination,
        operation_id="NOT-CANONICAL",
    )
    assert rejected.returncode != 0
    assert (destination / "NikaCore.exe").read_bytes() == before_destination
    assert (rollback / "NikaCore.exe").read_bytes() == before_rollback


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_malformed_rollback_operation_marker_fails_before_mutation(tmp_path: Path) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    bundle_v1 = _bundle(tmp_path / "v1", "v1")
    bundle_v2 = _bundle(tmp_path / "v2", "v2")
    destination = tmp_path / "install" / "Nika Core"
    rollback = destination.parent / f".{destination.name}.rollback"
    marker = destination.parent / f".{destination.name}.rollback-operation.json"

    assert _run(
        shell,
        script=SCRIPT,
        mode="Install",
        destination=destination,
        bundle=bundle_v1,
    ).returncode == 0
    assert _run(
        shell,
        script=SCRIPT,
        mode="Update",
        destination=destination,
        bundle=bundle_v2,
    ).returncode == 0

    marker.write_text('{"marker_version":1,"operation_id":', encoding="utf-8")
    before_destination = (destination / "NikaCore.exe").read_bytes()
    before_rollback = (rollback / "NikaCore.exe").read_bytes()
    rejected = _run(
        shell,
        script=SCRIPT,
        mode="Rollback",
        destination=destination,
        operation_id="4" * 32,
    )
    assert rejected.returncode != 0
    assert (destination / "NikaCore.exe").read_bytes() == before_destination
    assert (rollback / "NikaCore.exe").read_bytes() == before_rollback
