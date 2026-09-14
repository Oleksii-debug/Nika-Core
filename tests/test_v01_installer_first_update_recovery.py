from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from nika_core.packaging.release import build_release_manifest, write_release_manifest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install_nika_core.ps1"
SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"


def _manifest(bundle: Path) -> None:
    manifest = build_release_manifest(
        bundle,
        product="NikaCore",
        version="0.0.3",
        source_sha=SOURCE_SHA,
    )
    write_release_manifest(bundle, manifest)


def _bundle(root: Path, marker: str) -> Path:
    bundle = root / f"bundle-{marker}"
    bundle.mkdir(parents=True)
    (bundle / "NikaCore.exe").write_text(marker, encoding="utf-8")
    internal = bundle / "_internal"
    internal.mkdir()
    (internal / "runtime.dat").write_text(f"runtime-{marker}", encoding="utf-8")
    _manifest(bundle)
    return bundle


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def _run(
    shell: str,
    *,
    mode: str,
    destination: Path,
    bundle: Path | None = None,
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


def test_markerless_first_update_recovery_is_update_only_and_effect_adjacent() -> None:
    payload = SCRIPT.read_text(encoding="utf-8")
    start = payload.index('$rollbackRecoveryState = Resolve-NikaInterruptedRollback')
    end = payload.index('if ($Mode -eq "Rollback") {', start)
    recovery = payload[start:end]

    move = "[System.IO.Directory]::Move($rollbackPath, $destinationPath)"
    move_index = recovery.index(move)
    assert '$Mode -eq "Update"' in recovery
    assert '-not (Test-Path -LiteralPath $destinationPath)' in recovery
    assert '(Test-Path -LiteralPath $rollbackPath -PathType Container)' in recovery
    assert '-not (Test-Path -LiteralPath $retiredRollbackPath)' in recovery
    assert '-not (Test-Path -LiteralPath $rollbackSwapPath)' in recovery
    assert recovery.index("Assert-NikaReleaseBundle -BundleRoot $rollbackPath") < move_index
    assert recovery.index("Assert-NikaNoReparsePathChain -Path $destinationPath") < move_index
    assert recovery.index(
        "Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @(",
    ) < move_index
    assert recovery.rindex("Assert-NikaNoReparsePathChain -Path $rollbackPath", 0, move_index) < move_index
    assert recovery.index("Assert-NikaReleaseBundle -BundleRoot $destinationPath", move_index) > move_index


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_first_update_missing_destination_recovers_then_updates_once(tmp_path: Path) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    bundle_v1 = _bundle(tmp_path / "version-1", "v1")
    bundle_v2 = _bundle(tmp_path / "version-2", "v2")
    destination = tmp_path / "install" / "Nika Core"
    rollback = destination.parent / f".{destination.name}.rollback"
    retired = destination.parent / f".{destination.name}.rollback-retired"
    swap = destination.parent / f".{destination.name}.rollback-swap"

    installed = _run(
        shell,
        mode="Install",
        destination=destination,
        bundle=bundle_v1,
    )
    assert installed.returncode == 0, installed.stderr or installed.stdout

    # Durable geometry after a hard stop immediately after the first Update's
    # Destination -> Rollback move. There is no prior rollback, so neither
    # rollback-retired nor rollback-swap can identify this transaction.
    destination.rename(rollback)
    assert not destination.exists()
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert not retired.exists()
    assert not swap.exists()

    external = tmp_path / "external-sentinel"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("must-not-change", encoding="utf-8")

    resumed = _run(
        shell,
        mode="Update",
        destination=destination,
        bundle=bundle_v2,
    )
    assert resumed.returncode == 0, resumed.stderr or resumed.stdout
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v2"
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert not retired.exists()
    assert not swap.exists()
    assert sentinel.read_text(encoding="utf-8") == "must-not-change"


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_markerless_missing_destination_preserves_existing_rollback_mode(tmp_path: Path) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    bundle_v1 = _bundle(tmp_path / "version-1", "v1")
    destination = tmp_path / "install" / "Nika Core"
    rollback = destination.parent / f".{destination.name}.rollback"

    installed = _run(
        shell,
        mode="Install",
        destination=destination,
        bundle=bundle_v1,
    )
    assert installed.returncode == 0, installed.stderr or installed.stdout
    destination.rename(rollback)

    resumed = _run(shell, mode="Rollback", destination=destination)
    assert resumed.returncode == 0, resumed.stderr or resumed.stdout
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert not rollback.exists()
