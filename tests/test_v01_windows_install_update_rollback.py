from __future__ import annotations

import json
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
        version="0.0.2",
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
    env_overrides: dict[str, str] | None = None,
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
    environment = os.environ.copy()
    if env_overrides:
        environment.update(env_overrides)
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
        timeout=30,
        env=environment,
    )


def _post_activation_injection_needle(mode: str) -> str:
    if mode == "Install":
        return (
            "            Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths "
            "@($destinationPath, $rollbackPath, $stagePath, $failedInstallPath)\n"
            "            [System.IO.Directory]::Move($stagePath, $destinationPath)\n"
        )
    return (
        "            Assert-NikaDataMutationSeparation -DataRoot $dataRoot -MutationPaths @(\n"
        "                $destinationPath,\n"
        "                $rollbackPath,\n"
        "                $retiredRollbackPath,\n"
        "                $stagePath,\n"
        "                $failedActivationPath\n"
        "            )\n"
        "            [System.IO.Directory]::Move($stagePath, $destinationPath)\n"
    )


def test_installer_contract_reuses_manifest_and_never_elevates() -> None:
    payload = SCRIPT.read_text(encoding="utf-8")
    assert "release-manifest.json" in payload
    assert "Get-FileHash -LiteralPath" in payload
    assert "Get-ChildItem -LiteralPath" in payload
    assert "Copy-Item -LiteralPath" in payload
    assert "Bundle and destination must not overlap" in payload
    assert "System directory install is forbidden" in payload
    assert "Start-Process -Verb RunAs" not in payload
    assert "runas" not in payload.casefold()
    assert "[System.IO.Path]::GetRelativePath" not in payload
    assert "$item.PSIsContainer" in payload
    assert "function Assert-NikaNoReparsePathChain" in payload
    assert "function Assert-NikaDataMutationSeparation" in payload
    assert 'Assert-NikaNoReparsePathChain -Path $canonicalDataRoot' in payload
    assert 'MutationPaths @($destinationPath, $rollbackPath)' in payload
    assert 'Assert-NikaNoReparsePathChain -Path $BundleRoot' in payload
    assert 'Assert-NikaNoReparsePathChain -Path $destinationPath' in payload
    assert 'Assert-NikaReleaseBundle -BundleRoot $destinationPath' in payload
    assert 'Directory]::Move($destinationPath, $failedActivationPath)' in payload
    assert 'Directory]::Move($rollbackPath, $destinationPath)' in payload
    assert (
        'throw "Release bundle contains a reparse point."\n'
        "        }\n"
        "        if ($item.Length -ne [int64]$size)"
    ) in payload


@pytest.mark.parametrize("mode", ["Install", "Update"])
def test_post_activation_fault_injection_binding_is_unique(mode: str) -> None:
    payload = SCRIPT.read_text(encoding="utf-8")
    needle = _post_activation_injection_needle(mode)
    assert payload.count(needle) == 1, "fault injection must bind exactly one activation phase"


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_install_update_rollback_preserves_external_user_data(tmp_path: Path) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    bundle_v1 = _bundle(tmp_path / "версія 1", "v1")
    bundle_v2 = _bundle(tmp_path / "версія 2", "v2")
    destination = tmp_path / "Користувач" / "Nika Core"
    user_data = tmp_path / "Користувач" / "NikaCoreData" / "nika_core.db"
    user_data.parent.mkdir(parents=True)
    user_data.write_text("durable-user-data", encoding="utf-8")

    installed = _run(shell, mode="Install", destination=destination, bundle=bundle_v1)
    assert installed.returncode == 0, installed.stderr or installed.stdout
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v1"

    updated = _run(shell, mode="Update", destination=destination, bundle=bundle_v2)
    assert updated.returncode == 0, updated.stderr or updated.stdout
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v2"

    rollback = destination.parent / f".{destination.name}.rollback"
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v1"

    restored = _run(shell, mode="Rollback", destination=destination)
    assert restored.returncode == 0, restored.stderr or restored.stdout
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v2"
    assert user_data.read_text(encoding="utf-8") == "durable-user-data"


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_tampered_update_fails_before_installed_tree_mutates(tmp_path: Path) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    good = _bundle(tmp_path / "good", "v1")
    tampered = _bundle(tmp_path / "tampered", "v2")
    destination = tmp_path / "Install Root" / "Nika Core"

    installed = _run(shell, mode="Install", destination=destination, bundle=good)
    assert installed.returncode == 0, installed.stderr or installed.stdout

    (tampered / "NikaCore.exe").write_text("modified-after-manifest", encoding="utf-8")
    rejected = _run(shell, mode="Update", destination=destination, bundle=tampered)
    assert rejected.returncode != 0
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert not (destination.parent / f".{destination.name}.rollback").exists()


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
@pytest.mark.parametrize("mode", ["Install", "Update"])
def test_post_activation_reparse_failure_cleans_or_restores_destination(
    tmp_path: Path, mode: str,
) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    bundle_v1 = _bundle(tmp_path / "version-1", "v1")
    bundle_v2 = _bundle(tmp_path / "version-2", "v2")
    destination = tmp_path / "install" / "Nika Core"
    if mode == "Update":
        installed = _run(shell, mode="Install", destination=destination, bundle=bundle_v1)
        assert installed.returncode == 0, installed.stderr or installed.stdout

    junction_target = tmp_path / "external-target"
    junction_target.mkdir()
    sentinel = junction_target / "sentinel.txt"
    sentinel.write_text("must-not-change", encoding="utf-8")

    instrumented = tmp_path / "install_nika_core_fault.ps1"
    payload = SCRIPT.read_text(encoding="utf-8")
    # Bind the injection to the selected guarded activation phase. The
    # phase-specific failure path keeps Install and Update unambiguous even
    # though both ultimately move the staged candidate into destination.
    needle = _post_activation_injection_needle(mode)
    injection_marker = tmp_path / "junction-injected.marker"
    escaped_target = str(junction_target).replace("'", "''")
    escaped_marker = str(injection_marker).replace("'", "''")
    injected = needle + (
        "            [System.IO.Directory]::Move($destinationPath, ($destinationPath + '.candidate'))\n"
        f"            New-Item -ItemType Junction -Path $destinationPath -Target '{escaped_target}' | Out-Null\n"
        "            $injectedItem = Get-Item -LiteralPath $destinationPath -Force\n"
        "            if (($injectedItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -eq 0) { "
        "throw 'test junction injection did not create a reparse point' }\n"
        f"            Set-Content -LiteralPath '{escaped_marker}' -Value 'junction-ready' -NoNewline\n"
    )
    assert payload.count(needle) == 1, "fault injection must bind exactly one activation phase"
    instrumented.write_text(payload.replace(needle, injected, 1), encoding="utf-8")

    command = [
        shell,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(instrumented),
        "-Mode",
        mode,
        "-Destination",
        str(destination),
        "-BundlePath",
        str(bundle_v2),
    ]
    failed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
        timeout=30,
    )

    assert failed.returncode != 0, failed.stdout or failed.stderr
    assert injection_marker.read_text(encoding="utf-8") == "junction-ready"
    assert "Reparse points are forbidden" in failed.stderr
    if mode == "Update":
        assert destination.is_dir()
        assert not destination.is_symlink()
        assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    else:
        assert not destination.exists()
    assert sentinel.read_text(encoding="utf-8") == "must-not-change"
    rollback = destination.parent / f".{destination.name}.rollback"
    assert not rollback.exists()


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
@pytest.mark.parametrize(
    "relative",
    ["_internal/runtime.dat.", "_internal/runtime.dat ",
     "_internal./runtime.dat", "_internal /runtime.dat"],
)
def test_manifest_rejects_trailing_dot_or_space_before_filesystem_mutation(
    tmp_path: Path, relative: str,
) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    bundle = _bundle(tmp_path / "bundle", "v1")
    manifest_path = bundle / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next(item for item in manifest["files"] if item["path"] == "_internal/runtime.dat")
    entry["path"] = relative
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    destination = tmp_path / "new-install-root" / "Nika Core"

    rejected = _run(shell, mode="Install", destination=destination, bundle=bundle)
    assert rejected.returncode != 0, rejected.stdout or rejected.stderr
    assert "Release manifest contains an unsafe path" in rejected.stderr
    assert not destination.parent.exists()


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_installer_rejects_destination_junction_ancestor_before_mutation(tmp_path: Path) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    bundle = _bundle(tmp_path / "bundle", "v1")
    physical_parent = tmp_path / "physical-parent"
    physical_parent.mkdir()
    junction_parent = tmp_path / "junction-parent"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction_parent), str(physical_parent)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
        timeout=10,
    )
    if created.returncode != 0:
        pytest.skip("Windows runner does not permit junction creation")

    destination = junction_parent / "Nika Core"
    rejected = _run(shell, mode="Install", destination=destination, bundle=bundle)
    assert rejected.returncode != 0
    assert not (physical_parent / "Nika Core").exists()


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_installer_rejects_bundle_root_junction_before_mutation(tmp_path: Path) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    real_bundle = _bundle(tmp_path / "real-bundle", "v1")
    junction_bundle = tmp_path / "junction-bundle"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction_bundle), str(real_bundle)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
        timeout=10,
    )
    if created.returncode != 0:
        pytest.skip("Windows runner does not permit junction creation")

    destination = tmp_path / "install" / "Nika Core"
    rejected = _run(shell, mode="Install", destination=destination, bundle=junction_bundle)
    assert rejected.returncode != 0
    assert not destination.exists()


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_update_rejects_canonical_data_inside_rollback_sibling_before_mutation(
    tmp_path: Path,
) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    bundle_v1 = _bundle(tmp_path / "bundle-v1", "v1")
    bundle_v2 = _bundle(tmp_path / "bundle-v2", "v2")
    destination = tmp_path / "install" / "Nika Core"

    installed = _run(shell, mode="Install", destination=destination, bundle=bundle_v1)
    assert installed.returncode == 0, installed.stderr or installed.stdout

    rollback = destination.parent / f".{destination.name}.rollback"
    rollback.mkdir()
    sentinel = rollback / "user-data-sentinel.txt"
    sentinel.write_text("preserve-me", encoding="utf-8")
    configured_db = rollback / "nika_core.db"

    rejected = _run(
        shell,
        mode="Update",
        destination=destination,
        bundle=bundle_v2,
        env_overrides={"NIKA_DB_PATH": str(configured_db)},
    )

    assert rejected.returncode != 0, rejected.stdout or rejected.stderr
    assert "must not overlap the canonical Nika Core data root" in rejected.stderr
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert sentinel.read_text(encoding="utf-8") == "preserve-me"


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_installer_rejects_canonical_data_root_junction_alias_before_mutation(
    tmp_path: Path,
) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    bundle = _bundle(tmp_path / "bundle", "v1")
    physical_parent = tmp_path / "physical-parent"
    physical_parent.mkdir()
    data_alias = tmp_path / "data-alias"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(data_alias), str(physical_parent)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
        timeout=10,
    )
    if created.returncode != 0:
        pytest.skip("Windows runner does not permit junction creation")

    destination = physical_parent / "Nika Core"
    configured_db = data_alias / "Nika Core" / "nika_core.db"
    rejected = _run(
        shell,
        mode="Install",
        destination=destination,
        bundle=bundle,
        env_overrides={"NIKA_DB_PATH": str(configured_db)},
    )

    assert rejected.returncode != 0, rejected.stdout or rejected.stderr
    assert "Reparse points are forbidden in installer path authority" in rejected.stderr
    assert not destination.exists()
