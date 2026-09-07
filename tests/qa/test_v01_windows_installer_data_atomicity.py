from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from nika_core.config import AppConfig
from nika_core.packaging.release import build_release_manifest, write_release_manifest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "install_nika_core.ps1"
SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


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


def _run(
    shell: str,
    *,
    script: Path = SCRIPT,
    mode: str,
    destination: Path,
    bundle: Path | None = None,
    env: dict[str, str] | None = None,
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
        timeout=30,
        env=env,
    )


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
@pytest.mark.parametrize("relationship", ("equal", "descendant", "ancestor"))
def test_install_rejects_canonical_user_data_overlap_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relationship: str,
) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    if relationship == "ancestor":
        destination = tmp_path / "Користувач" / "Shared App And Data Root"
        local_app_data = destination / "Local AppData"
    else:
        local_app_data = tmp_path / "Користувач" / relationship / "Local AppData"
        canonical_root = local_app_data / "NikaCore"
        destination = (
            canonical_root
            if relationship == "equal"
            else canonical_root / "Programs" / "Nika Core"
        )

    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.delenv("NIKA_DB_PATH", raising=False)
    monkeypatch.delenv("NIKA_DATABASE_PATH", raising=False)

    config = AppConfig()
    canonical_data_root = config.database_path.parent
    assert canonical_data_root == local_app_data / "NikaCore"
    if relationship == "equal":
        assert destination == canonical_data_root
    elif relationship == "descendant":
        assert canonical_data_root in destination.parents
    else:
        assert destination in canonical_data_root.parents

    bundle = _bundle(tmp_path / f"release-{relationship}", "v1")
    result = _run(
        shell,
        mode="Install",
        destination=destination,
        bundle=bundle,
        env=os.environ.copy(),
    )

    assert result.returncode != 0
    assert not destination.exists()
    assert bundle.is_dir()
    assert (bundle / "NikaCore.exe").read_text(encoding="utf-8") == "v1"


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
@pytest.mark.parametrize("database_alias", ("NIKA_DB_PATH", "NIKA_DATABASE_PATH"))
def test_install_rejects_explicit_database_path_inside_destination(
    tmp_path: Path,
    database_alias: str,
) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    bundle = _bundle(tmp_path / f"release-explicit-db-{database_alias}", "v1")
    destination = tmp_path / "Custom Install" / database_alias / "Nika Core"
    explicit_database = destination / "durable" / "nika_core.db"
    env = os.environ.copy()
    env.pop("NIKA_DB_PATH", None)
    env.pop("NIKA_DATABASE_PATH", None)
    env[database_alias] = str(explicit_database)

    result = _run(
        shell,
        mode="Install",
        destination=destination,
        bundle=bundle,
        env=env,
    )

    assert result.returncode != 0
    assert not destination.exists()


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_install_allows_normal_programs_destination_separate_from_canonical_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    local_app_data = tmp_path / "Користувач" / "Local AppData"
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.delenv("NIKA_DB_PATH", raising=False)
    monkeypatch.delenv("NIKA_DATABASE_PATH", raising=False)

    config = AppConfig()
    canonical_data_root = config.database_path.parent
    destination = local_app_data / "Programs" / "NikaCore"
    assert canonical_data_root == local_app_data / "NikaCore"
    assert canonical_data_root not in destination.parents
    assert destination not in canonical_data_root.parents

    bundle = _bundle(tmp_path / "release-normal-programs", "v1")
    result = _run(
        shell,
        mode="Install",
        destination=destination,
        bundle=bundle,
        env=os.environ.copy(),
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert not canonical_data_root.exists()


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_failed_install_post_activation_verification_leaves_no_active_destination(
    tmp_path: Path,
) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    bundle = _bundle(tmp_path / "release", "v1")
    destination = tmp_path / "Install Root" / "Nika Core"

    payload = SCRIPT.read_text(encoding="utf-8")
    needle = (
        "        [System.IO.Directory]::Move($stagePath, $destinationPath)\n"
        "        Assert-NikaNoReparsePathChain -Path $destinationPath\n"
        "        Assert-NikaReleaseBundle -BundleRoot $destinationPath\n"
    )
    marker = tmp_path / "post-activation-fault.marker"
    escaped_marker = str(marker).replace("'", "''")
    injected = (
        "        [System.IO.Directory]::Move($stagePath, $destinationPath)\n"
        f"        Set-Content -LiteralPath '{escaped_marker}' -Value 'activated' -NoNewline\n"
        "        Set-Content -LiteralPath (Join-Path $destinationPath 'NikaCore.exe') "
        "-Value 'tampered-after-activation' -NoNewline\n"
        "        Assert-NikaNoReparsePathChain -Path $destinationPath\n"
        "        Assert-NikaReleaseBundle -BundleRoot $destinationPath\n"
    )
    assert needle in payload
    instrumented = tmp_path / "install_nika_core_install_fault.ps1"
    instrumented.write_text(payload.replace(needle, injected, 1), encoding="utf-8")

    result = _run(
        shell,
        script=instrumented,
        mode="Install",
        destination=destination,
        bundle=bundle,
    )

    assert result.returncode != 0
    assert marker.read_text(encoding="utf-8") == "activated"
    assert not destination.exists()
    assert not list(destination.parent.glob(f".{destination.name}.staging-*"))


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_failed_third_rollback_move_restores_verified_precommand_pair(
    tmp_path: Path,
) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    bundle_v1 = _bundle(tmp_path / "release-v1", "v1")
    bundle_v2 = _bundle(tmp_path / "release-v2", "v2")
    destination = tmp_path / "Install Root" / "Nika Core"

    installed = _run(shell, mode="Install", destination=destination, bundle=bundle_v1)
    assert installed.returncode == 0, installed.stderr or installed.stdout
    updated = _run(shell, mode="Update", destination=destination, bundle=bundle_v2)
    assert updated.returncode == 0, updated.stderr or updated.stdout

    rollback = destination.parent / f".{destination.name}.rollback"
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v2"
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v1"

    payload = SCRIPT.read_text(encoding="utf-8")
    needle = "        [System.IO.Directory]::Move($swapPath, $rollbackPath)\n"
    marker = tmp_path / "rollback-third-move-fault.marker"
    escaped_marker = str(marker).replace("'", "''")
    injected = (
        f"        Set-Content -LiteralPath '{escaped_marker}' -Value 'before-third-move' -NoNewline\n"
        "        throw 'injected third rollback move failure'\n"
    )
    assert needle in payload
    instrumented = tmp_path / "install_nika_core_rollback_fault.ps1"
    instrumented.write_text(payload.replace(needle, injected, 1), encoding="utf-8")

    failed = _run(
        shell,
        script=instrumented,
        mode="Rollback",
        destination=destination,
    )

    assert failed.returncode != 0
    assert marker.read_text(encoding="utf-8") == "before-third-move"
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v2"
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert not list(destination.parent.glob(f".{destination.name}.swap-*"))
