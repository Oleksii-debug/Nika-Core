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
    script: Path,
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
        str(script),
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


def _prepare_missing_destination_recovery_state(
    tmp_path: Path,
    shell: str,
) -> tuple[Path, Path, Path, Path]:
    bundle_v1 = _bundle(tmp_path / "version-1", "v1")
    bundle_v2 = _bundle(tmp_path / "version-2", "v2")
    bundle_v3 = _bundle(tmp_path / "version-3", "v3")
    destination = tmp_path / "install" / "Nika Core"
    rollback = destination.parent / f".{destination.name}.rollback"
    retired = destination.parent / f".{destination.name}.rollback-retired"

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

    rollback.rename(retired)
    destination.rename(rollback)
    assert not destination.exists()
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v2"
    assert (retired / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    return bundle_v3, destination, rollback, retired


def test_update_replacement_order_preserves_prior_rollback_until_activation() -> None:
    payload = SCRIPT.read_text(encoding="utf-8")
    update = payload[payload.index("$hadPriorRollback ="):]

    retire_prior = "[System.IO.Directory]::Move($rollbackPath, $retiredRollbackPath)"
    establish_replacement = "[System.IO.Directory]::Move($destinationPath, $rollbackPath)"
    activate_candidate = "[System.IO.Directory]::Move($stagePath, $destinationPath)"
    retire_after_success = "Remove-Item -LiteralPath $retiredRollbackPath -Recurse -Force"

    assert "$retiredRollbackPath = Join-Path $parent (\".$leaf.rollback-retired\")" in payload
    assert "function Resolve-NikaInterruptedUpdate" in payload
    assert update.count(retire_prior) == 1
    assert update.count(establish_replacement) == 1
    assert update.count(activate_candidate) == 1
    assert retire_prior in update
    assert establish_replacement in update
    assert activate_candidate in update
    assert retire_after_success in update
    assert update.index(retire_prior) < update.index(establish_replacement)
    assert update.index(establish_replacement) < update.index(activate_candidate)
    assert update.index(activate_candidate) < update.index(retire_after_success)
    assert "Remove-Item -LiteralPath $rollbackPath -Recurse -Force" not in update


def test_missing_destination_recovery_revalidates_all_authority_before_first_move() -> None:
    payload = SCRIPT.read_text(encoding="utf-8")
    recovery = payload[
        payload.index("function Resolve-NikaInterruptedUpdate") :
        payload.index('if ([string]::IsNullOrWhiteSpace($Destination))')
    ]
    branch = recovery[
        recovery.index("elseif (-not $hasDestination -and $hasRollback) {") :
        recovery.index("elseif ($hasDestination -and $hasRollback) {")
    ]
    first_move = "[System.IO.Directory]::Move($RollbackPath, $DestinationPath)"
    first_move_index = branch.index(first_move)

    required_before_effect = (
        "Assert-NikaNoReparsePathChain -Path $RetiredRollbackPath",
        "Assert-NikaReleaseBundle -BundleRoot $RetiredRollbackPath",
        "Assert-NikaNoReparsePathChain -Path $RollbackPath",
        "Assert-NikaReleaseBundle -BundleRoot $RollbackPath",
        "Assert-NikaNoReparsePathChain -Path $DestinationPath",
        "Assert-NikaDataMutationSeparation -DataRoot $DataRoot -MutationPaths @(",
    )
    for required in required_before_effect:
        assert branch.index(required) < first_move_index


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_failed_third_update_restores_active_and_prior_rollback(tmp_path: Path) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    bundle_v1 = _bundle(tmp_path / "version-1", "v1")
    bundle_v2 = _bundle(tmp_path / "version-2", "v2")
    bundle_v3 = _bundle(tmp_path / "version-3", "v3")
    destination = tmp_path / "install" / "Nika Core"
    rollback = destination.parent / f".{destination.name}.rollback"
    retired = destination.parent / f".{destination.name}.rollback-retired"

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
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v2"
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v1"

    payload = SCRIPT.read_text(encoding="utf-8")
    needle = (
        "            [System.IO.Directory]::Move($destinationPath, $rollbackPath)\n"
        "            $activeMovedToRollback = $true\n"
    )
    assert payload.count(needle) == 1
    instrumented = tmp_path / "install_nika_core_atomicity_fault.ps1"
    instrumented.write_text(
        payload.replace(
            needle,
            "            throw \"synthetic rollback replacement boundary failure\"\n" + needle,
            1,
        ),
        encoding="utf-8",
    )

    failed = _run(
        shell,
        script=instrumented,
        mode="Update",
        destination=destination,
        bundle=bundle_v3,
    )
    assert failed.returncode != 0
    assert "synthetic rollback replacement boundary failure" in failed.stderr
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v2"
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert not retired.exists()


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_interrupted_retired_rollback_is_recovered_before_next_update(tmp_path: Path) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    bundle_v1 = _bundle(tmp_path / "version-1", "v1")
    bundle_v2 = _bundle(tmp_path / "version-2", "v2")
    bundle_v3 = _bundle(tmp_path / "version-3", "v3")
    destination = tmp_path / "install" / "Nika Core"
    rollback = destination.parent / f".{destination.name}.rollback"
    retired = destination.parent / f".{destination.name}.rollback-retired"

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

    rollback.rename(retired)
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v2"
    assert (retired / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert not rollback.exists()

    resumed = _run(
        shell,
        script=SCRIPT,
        mode="Update",
        destination=destination,
        bundle=bundle_v3,
    )
    assert resumed.returncode == 0, resumed.stderr or resumed.stdout
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v3"
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v2"
    assert not retired.exists()


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_missing_destination_recovery_control_restores_then_updates(tmp_path: Path) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    bundle_v3, destination, rollback, retired = _prepare_missing_destination_recovery_state(
        tmp_path,
        shell,
    )
    resumed = _run(
        shell,
        script=SCRIPT,
        mode="Update",
        destination=destination,
        bundle=bundle_v3,
    )
    assert resumed.returncode == 0, resumed.stderr or resumed.stdout
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v3"
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v2"
    assert not retired.exists()


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_missing_destination_recovery_rejects_retired_junction_before_first_move(
    tmp_path: Path,
) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    bundle_v3, destination, rollback, retired = _prepare_missing_destination_recovery_state(
        tmp_path,
        shell,
    )
    external_target = tmp_path / "external-retired-target"
    external_target.mkdir()
    sentinel = external_target / "sentinel.txt"
    sentinel.write_text("must-not-change", encoding="utf-8")

    payload = SCRIPT.read_text(encoding="utf-8")
    needle = "    elseif (-not $hasDestination -and $hasRollback) {\n"
    assert payload.count(needle) == 1
    escaped_target = str(external_target).replace("'", "''")
    injected = needle + (
        "        $raceOriginal = $RetiredRollbackPath + '.race-original'\n"
        "        [System.IO.Directory]::Move($RetiredRollbackPath, $raceOriginal)\n"
        f"        New-Item -ItemType Junction -Path $RetiredRollbackPath -Target '{escaped_target}' | Out-Null\n"
        "        $raceRetired = Get-Item -LiteralPath $RetiredRollbackPath -Force\n"
        "        if (($raceRetired.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -eq 0) {\n"
        "            throw 'test retired junction injection did not create a reparse point'\n"
        "        }\n"
    )
    instrumented = tmp_path / "install_nika_core_recovery_toctou_fault.ps1"
    instrumented.write_text(payload.replace(needle, injected, 1), encoding="utf-8")
    race_original = Path(str(retired) + ".race-original")

    failed = _run(
        shell,
        script=instrumented,
        mode="Update",
        destination=destination,
        bundle=bundle_v3,
    )
    assert failed.returncode != 0, failed.stdout
    assert "Reparse points are forbidden" in failed.stderr
    assert not destination.exists()
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v2"
    assert (race_original / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert sentinel.read_text(encoding="utf-8") == "must-not-change"

    removed = subprocess.run(
        ["cmd", "/c", "rmdir", str(retired)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
        timeout=10,
    )
    assert removed.returncode == 0, removed.stderr or removed.stdout
    race_original.rename(retired)
