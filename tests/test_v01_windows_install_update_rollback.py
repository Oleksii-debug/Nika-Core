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
        timeout=30,
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
    assert (
        'throw "Release bundle contains a reparse point."\n'
        "        }\n"
        "        if ($item.Length -ne [int64]$size)"
    ) in payload


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
