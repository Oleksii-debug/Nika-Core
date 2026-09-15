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


def _committed_first_update_receipt(destination: Path) -> Path:
    prefix = f".{destination.name}.first-update-"
    receipts = [
        entry
        for entry in destination.parent.iterdir()
        if entry.name.lower().startswith(prefix.lower())
    ]
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.is_dir()
    target_digest = receipt.name[len(prefix) :]
    assert len(target_digest) == 64
    assert all(character in "0123456789abcdef" for character in target_digest)
    assert not (receipt / "candidate").exists()
    return receipt


def _retire_committed_first_update_receipt(destination: Path) -> None:
    # A real later Update with a different manifest, or a real Rollback, removes
    # the durable first-update completion receipt before its first destructive
    # move. Manual crash fixtures must cross the same boundary before creating
    # a later transaction's interrupted geometry.
    _committed_first_update_receipt(destination).rmdir()


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

    _retire_committed_first_update_receipt(destination)
    rollback.rename(retired)
    destination.rename(rollback)
    assert not destination.exists()
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v2"
    assert (retired / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    return bundle_v3, destination, rollback, retired


def _prepare_rollback_pair(
    tmp_path: Path,
    shell: str,
    *,
    retire_first_update_receipt: bool = True,
) -> tuple[Path, Path, Path]:
    bundle_v1 = _bundle(tmp_path / "rollback-version-1", "v1")
    bundle_v2 = _bundle(tmp_path / "rollback-version-2", "v2")
    destination = tmp_path / "rollback-install" / "Nika Core"
    rollback = destination.parent / f".{destination.name}.rollback"
    swap = destination.parent / f".{destination.name}.rollback-swap"

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
    assert not swap.exists()
    if retire_first_update_receipt:
        _retire_committed_first_update_receipt(destination)
    return destination, rollback, swap


def test_update_replacement_order_preserves_prior_rollback_until_activation() -> None:
    payload = SCRIPT.read_text(encoding="utf-8")
    update_start = payload.index("$failedActivationPath = Join-Path $parent")
    update = payload[update_start : payload.index("\nfinally {", update_start)]

    retire_prior = "[System.IO.Directory]::Move($rollbackPath, $retiredRollbackPath)"
    establish_replacement = "[System.IO.Directory]::Move($destinationPath, $rollbackPath)"
    activate_candidate = "[System.IO.Directory]::Move($stagePath, $destinationPath)"
    retire_after_success = "Remove-NikaTreeNoFollow -Path $retiredRollbackPath"

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
    assert "Remove-Item -LiteralPath $retiredRollbackPath -Recurse -Force" not in update


def test_missing_destination_recovery_revalidates_all_authority_before_first_move() -> None:
    payload = SCRIPT.read_text(encoding="utf-8")
    recovery = payload[
        payload.index("function Resolve-NikaInterruptedUpdate") :
        payload.index("function Resolve-NikaInterruptedRollback")
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


def test_rollback_swap_is_deterministic_and_reconciled_before_mode_dispatch() -> None:
    payload = SCRIPT.read_text(encoding="utf-8")
    recovery_start = payload.index("function Resolve-NikaInterruptedRollback")
    dispatch = payload.index('if ($Mode -eq "Rollback") {')
    recovery_call = payload.index("$rollbackRecoveryState = Resolve-NikaInterruptedRollback")

    assert '$rollbackSwapPath = Join-Path $parent (".$leaf.rollback-swap")' in payload
    assert '".$leaf.swap-$([Guid]::NewGuid().ToString(\'N\'))"' not in payload
    assert recovery_start < recovery_call < dispatch
    assert "Multiple interrupted installer transactions are present" in payload
    assert '"completed-rollback"' not in payload[recovery_start:dispatch]


def test_rollback_recovery_freezes_both_hard_interruption_geometries() -> None:
    payload = SCRIPT.read_text(encoding="utf-8")
    recovery = payload[
        payload.index("function Resolve-NikaInterruptedRollback") :
        payload.index('if ([string]::IsNullOrWhiteSpace($Destination))')
    ]
    first_window = recovery[
        recovery.index("if (-not $hasDestination -and $hasRollback) {") :
        recovery.index("elseif ($hasDestination -and -not $hasRollback) {")
    ]
    second_window = recovery[
        recovery.index("elseif ($hasDestination -and -not $hasRollback) {") :
        recovery.index("else {", recovery.index("elseif ($hasDestination -and -not $hasRollback) {"))
    ]

    assert "[System.IO.Directory]::Move($SwapPath, $DestinationPath)" in first_window
    assert '$recoveryState = "restored-precommand"' in first_window
    restore_rollback = "[System.IO.Directory]::Move($DestinationPath, $RollbackPath)"
    restore_active = "[System.IO.Directory]::Move($SwapPath, $DestinationPath)"
    assert restore_rollback in second_window
    assert restore_active in second_window
    assert second_window.index(restore_rollback) < second_window.index(restore_active)
    assert '$recoveryState = "restored-precommand"' in second_window
    assert '"completed-rollback"' not in second_window
    assert "Assert-NikaNoReparsePathChain -Path $SwapPath" in first_window
    assert "Assert-NikaReleaseBundle -BundleRoot $SwapPath" in first_window
    assert second_window.count("Assert-NikaNoReparsePathChain -Path $SwapPath") >= 2
    assert second_window.count("Assert-NikaReleaseBundle -BundleRoot $SwapPath") >= 2
    assert second_window.count("Assert-NikaDataMutationSeparation -DataRoot $DataRoot -MutationPaths @(") >= 2


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

    _retire_committed_first_update_receipt(destination)
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


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_rollback_restart_after_active_staged_crash_completes_once(tmp_path: Path) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    destination, rollback, swap = _prepare_rollback_pair(tmp_path, shell)
    destination.rename(swap)
    assert not destination.exists()
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert (swap / "NikaCore.exe").read_text(encoding="utf-8") == "v2"

    resumed = _run(shell, script=SCRIPT, mode="Rollback", destination=destination)
    assert resumed.returncode == 0, resumed.stderr or resumed.stdout
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v2"
    assert not swap.exists()


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_rollback_restart_after_activation_crash_does_not_replay_swap(tmp_path: Path) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    destination, rollback, swap = _prepare_rollback_pair(tmp_path, shell)
    destination.rename(swap)
    rollback.rename(destination)
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert not rollback.exists()
    assert (swap / "NikaCore.exe").read_text(encoding="utf-8") == "v2"

    resumed = _run(shell, script=SCRIPT, mode="Rollback", destination=destination)
    assert resumed.returncode == 0, resumed.stderr or resumed.stdout
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v2"
    assert not swap.exists()


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_rollback_recovery_restart_after_first_second_window_move_is_idempotent(
    tmp_path: Path,
) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    destination, rollback, swap = _prepare_rollback_pair(tmp_path, shell)
    destination.rename(swap)
    rollback.rename(destination)
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert not rollback.exists()
    assert (swap / "NikaCore.exe").read_text(encoding="utf-8") == "v2"

    payload = SCRIPT.read_text(encoding="utf-8")
    second_window = payload.index("elseif ($hasDestination -and -not $hasRollback) {")
    first_recovery_move = "        [System.IO.Directory]::Move($DestinationPath, $RollbackPath)"
    move_index = payload.index(first_recovery_move, second_window)
    insert_at = move_index + len(first_recovery_move)
    instrumented_payload = payload[:insert_at] + "\n        exit 91" + payload[insert_at:]
    instrumented = tmp_path / "install_nika_core_rollback_recovery_crash1.ps1"
    instrumented.write_text(instrumented_payload, encoding="utf-8")

    crashed = _run(shell, script=instrumented, mode="Rollback", destination=destination)
    assert crashed.returncode == 91, crashed.stderr or crashed.stdout
    assert not destination.exists()
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert (swap / "NikaCore.exe").read_text(encoding="utf-8") == "v2"

    resumed = _run(shell, script=SCRIPT, mode="Rollback", destination=destination)
    assert resumed.returncode == 0, resumed.stderr or resumed.stdout
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v2"
    assert not swap.exists()


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_rollback_recovery_restart_after_final_second_window_move_runs_mode_once(
    tmp_path: Path,
) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    destination, rollback, swap = _prepare_rollback_pair(tmp_path, shell)
    destination.rename(swap)
    rollback.rename(destination)

    payload = SCRIPT.read_text(encoding="utf-8")
    second_window = payload.index("elseif ($hasDestination -and -not $hasRollback) {")
    final_recovery_move = "        [System.IO.Directory]::Move($SwapPath, $DestinationPath)"
    move_index = payload.index(final_recovery_move, second_window)
    insert_at = move_index + len(final_recovery_move)
    instrumented_payload = payload[:insert_at] + "\n        exit 92" + payload[insert_at:]
    instrumented = tmp_path / "install_nika_core_rollback_recovery_crash2.ps1"
    instrumented.write_text(instrumented_payload, encoding="utf-8")

    crashed = _run(shell, script=instrumented, mode="Rollback", destination=destination)
    assert crashed.returncode == 92, crashed.stderr or crashed.stdout
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v2"
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert not swap.exists()

    resumed = _run(shell, script=SCRIPT, mode="Rollback", destination=destination)
    assert resumed.returncode == 0, resumed.stderr or resumed.stdout
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v2"
    assert not swap.exists()


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_rollback_restart_rejects_invalid_owned_transient_without_mutation(tmp_path: Path) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    destination, rollback, swap = _prepare_rollback_pair(tmp_path, shell)
    swap.mkdir()
    (swap / "not-a-release.txt").write_text("invalid", encoding="utf-8")

    rejected = _run(shell, script=SCRIPT, mode="Rollback", destination=destination)
    assert rejected.returncode != 0
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v2"
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert (swap / "not-a-release.txt").read_text(encoding="utf-8") == "invalid"


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_rollback_restart_rejects_swap_junction_without_touching_external_target(tmp_path: Path) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    destination, rollback, swap = _prepare_rollback_pair(tmp_path, shell)
    preserved_active = Path(str(swap) + ".preserved-active")
    destination.rename(preserved_active)
    external_target = tmp_path / "external-rollback-swap-target"
    external_target.mkdir()
    sentinel = external_target / "sentinel.txt"
    sentinel.write_text("must-not-change", encoding="utf-8")

    linked = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(swap), str(external_target)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
        timeout=10,
    )
    assert linked.returncode == 0, linked.stderr or linked.stdout

    rejected = _run(shell, script=SCRIPT, mode="Rollback", destination=destination)
    assert rejected.returncode != 0
    assert "Reparse points are forbidden" in rejected.stderr
    assert not destination.exists()
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert (preserved_active / "NikaCore.exe").read_text(encoding="utf-8") == "v2"
    assert sentinel.read_text(encoding="utf-8") == "must-not-change"

    removed = subprocess.run(
        ["cmd", "/c", "rmdir", str(swap)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
        timeout=10,
    )
    assert removed.returncode == 0, removed.stderr or removed.stdout


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_multiple_transaction_authorities_remain_fail_closed(tmp_path: Path) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    destination, rollback, swap = _prepare_rollback_pair(
        tmp_path,
        shell,
        retire_first_update_receipt=False,
    )
    receipt = _committed_first_update_receipt(destination)
    destination.rename(swap)

    rejected = _run(shell, script=SCRIPT, mode="Rollback", destination=destination)
    assert rejected.returncode != 0, rejected.stdout
    assert "Multiple interrupted installer transactions are present" in rejected.stderr
    assert receipt.exists()
    assert not destination.exists()
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert (swap / "NikaCore.exe").read_text(encoding="utf-8") == "v2"
