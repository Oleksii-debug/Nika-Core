from __future__ import annotations

import hashlib
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


def _manifest_digest(bundle: Path) -> str:
    return hashlib.sha256((bundle / "release-manifest.json").read_bytes()).hexdigest()


def _transaction(destination: Path, bundle: Path, *, with_candidate: bool) -> tuple[Path, Path]:
    transaction = destination.parent / (
        f".{destination.name}.first-update-{_manifest_digest(bundle)}"
    )
    transaction.mkdir()
    candidate = transaction / "candidate"
    if with_candidate:
        shutil.copytree(bundle, candidate)
    return transaction, candidate


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


def test_first_update_recovery_requires_durable_manifest_bound_authority() -> None:
    payload = SCRIPT.read_text(encoding="utf-8")

    assert "function Get-NikaReleaseManifestDigest" in payload
    assert "function Get-NikaFirstUpdateTransaction" in payload
    assert "function Resolve-NikaInterruptedFirstUpdate" in payload
    assert '".$leaf.first-update-$bundleManifestDigest"' in payload
    assert '$stagePath = Join-Path $firstUpdateTransactionPath "candidate"' in payload
    assert '$firstUpdateRecoveryState -eq "committed"' in payload
    assert "$bundleManifestDigest -ceq [string]$firstUpdateTransaction.TargetDigest" in payload
    assert "Markerless first-update recovery" not in payload


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_first_update_pre_effect_transaction_is_aborted_then_update_runs_once(tmp_path: Path) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    bundle_v1 = _bundle(tmp_path / "version-1", "v1")
    bundle_v2 = _bundle(tmp_path / "version-2", "v2")
    destination = tmp_path / "install" / "Nika Core"
    rollback = destination.parent / f".{destination.name}.rollback"

    installed = _run(shell, mode="Install", destination=destination, bundle=bundle_v1)
    assert installed.returncode == 0, installed.stderr or installed.stdout

    transaction, candidate = _transaction(destination, bundle_v2, with_candidate=True)
    assert candidate.exists()
    assert not rollback.exists()

    resumed = _run(shell, mode="Update", destination=destination, bundle=bundle_v2)
    assert resumed.returncode == 0, resumed.stderr or resumed.stdout
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v2"
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert transaction.exists()
    assert not (transaction / "candidate").exists()


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_first_update_crash_after_first_move_restores_then_updates_once(tmp_path: Path) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    bundle_v1 = _bundle(tmp_path / "version-1", "v1")
    bundle_v2 = _bundle(tmp_path / "version-2", "v2")
    destination = tmp_path / "install" / "Nika Core"
    rollback = destination.parent / f".{destination.name}.rollback"
    retired = destination.parent / f".{destination.name}.rollback-retired"
    swap = destination.parent / f".{destination.name}.rollback-swap"

    installed = _run(shell, mode="Install", destination=destination, bundle=bundle_v1)
    assert installed.returncode == 0, installed.stderr or installed.stdout

    transaction, candidate = _transaction(destination, bundle_v2, with_candidate=True)
    destination.rename(rollback)
    assert not destination.exists()
    assert candidate.exists()

    external = tmp_path / "external-sentinel"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("must-not-change", encoding="utf-8")

    resumed = _run(shell, mode="Update", destination=destination, bundle=bundle_v2)
    assert resumed.returncode == 0, resumed.stderr or resumed.stdout
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v2"
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert transaction.exists()
    assert not (transaction / "candidate").exists()
    assert not retired.exists()
    assert not swap.exists()
    assert sentinel.read_text(encoding="utf-8") == "must-not-change"


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_first_update_crash_after_activation_retries_idempotently(tmp_path: Path) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    bundle_v1 = _bundle(tmp_path / "version-1", "v1")
    bundle_v2 = _bundle(tmp_path / "version-2", "v2")
    destination = tmp_path / "install" / "Nika Core"
    rollback = destination.parent / f".{destination.name}.rollback"
    retired = destination.parent / f".{destination.name}.rollback-retired"

    installed = _run(shell, mode="Install", destination=destination, bundle=bundle_v1)
    assert installed.returncode == 0, installed.stderr or installed.stdout

    transaction, candidate = _transaction(destination, bundle_v2, with_candidate=True)
    destination.rename(rollback)
    candidate.rename(destination)
    assert transaction.exists()
    assert not candidate.exists()
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v2"
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v1"

    resumed = _run(shell, mode="Update", destination=destination, bundle=bundle_v2)
    assert resumed.returncode == 0, resumed.stderr or resumed.stdout
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v2"
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert transaction.exists()
    assert list(transaction.iterdir()) == []
    assert not retired.exists()


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_markerless_missing_destination_update_fails_closed(tmp_path: Path) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    bundle_v1 = _bundle(tmp_path / "version-1", "v1")
    bundle_v2 = _bundle(tmp_path / "version-2", "v2")
    destination = tmp_path / "install" / "Nika Core"
    rollback = destination.parent / f".{destination.name}.rollback"

    installed = _run(shell, mode="Install", destination=destination, bundle=bundle_v1)
    assert installed.returncode == 0, installed.stderr or installed.stdout
    destination.rename(rollback)

    failed = _run(shell, mode="Update", destination=destination, bundle=bundle_v2)
    assert failed.returncode != 0, failed.stdout
    assert "Update requires an existing installed application" in failed.stderr
    assert not destination.exists()
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert not list(destination.parent.glob(f".{destination.name}.first-update-*"))


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_markerless_missing_destination_preserves_explicit_rollback_mode(tmp_path: Path) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    bundle_v1 = _bundle(tmp_path / "version-1", "v1")
    destination = tmp_path / "install" / "Nika Core"
    rollback = destination.parent / f".{destination.name}.rollback"

    installed = _run(shell, mode="Install", destination=destination, bundle=bundle_v1)
    assert installed.returncode == 0, installed.stderr or installed.stdout
    destination.rename(rollback)

    resumed = _run(shell, mode="Rollback", destination=destination)
    assert resumed.returncode == 0, resumed.stderr or resumed.stdout
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert not rollback.exists()
