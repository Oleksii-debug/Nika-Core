from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from nika_core.packaging.release import build_release_manifest, write_release_manifest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "install_nika_core.ps1"
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
        version="0.0.2",
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
    environment: dict[str, str] | None = None,
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
        env=environment,
        timeout=30,
    )


def _instrument_post_activation_junction(script: Path, target: Path) -> Path:
    payload = SCRIPT.read_text(encoding="utf-8")
    needle = (
        "            [System.IO.Directory]::Move($stagePath, $destinationPath)\n"
        "            Assert-NikaNoReparsePathChain -Path $destinationPath"
    )
    assert payload.count(needle) == 1, "QA injection point must stay exact and unambiguous"
    replacement = (
        "            [System.IO.Directory]::Move($stagePath, $destinationPath)\n"
        "            if (-not [string]::IsNullOrWhiteSpace($env:NIKA_QA_REPARSE_TARGET)) {\n"
        "                $qaIncomingPath = Join-Path $parent (\".$leaf.qa-incoming-$([Guid]::NewGuid().ToString('N'))\")\n"
        "                [System.IO.Directory]::Move($destinationPath, $qaIncomingPath)\n"
        "                & cmd.exe /d /c mklink /J \"$destinationPath\" \"$env:NIKA_QA_REPARSE_TARGET\" | Out-Null\n"
        "                if ($LASTEXITCODE -ne 0) { throw \"QA post-activation junction injection failed.\" }\n"
        "            }\n"
        "            Assert-NikaNoReparsePathChain -Path $destinationPath"
    )
    script.write_text(payload.replace(needle, replacement), encoding="utf-8")
    return script


@pytest.mark.skipif(os.name != "nt", reason="real post-activation junction proof is Windows-only")
def test_update_reparse_failure_restores_previous_release_without_target_traversal(
    tmp_path: Path,
) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    bundle_v1 = _bundle(tmp_path / "версія 1", "v1")
    bundle_v2 = _bundle(tmp_path / "версія 2", "v2")
    destination = tmp_path / "Користувач" / "Nika Core"

    installed = _run(
        shell,
        script=SCRIPT,
        mode="Install",
        destination=destination,
        bundle=bundle_v1,
    )
    assert installed.returncode == 0, installed.stderr or installed.stdout
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v1"

    external_target = tmp_path / "НЕ ЧІПАТИ target"
    external_target.mkdir()
    sentinel = external_target / "sentinel.txt"
    sentinel.write_text("must-survive", encoding="utf-8")

    instrumented = _instrument_post_activation_junction(
        tmp_path / "install_nika_core_fault_injected.ps1",
        external_target,
    )
    environment = dict(os.environ)
    environment["NIKA_QA_REPARSE_TARGET"] = str(external_target)

    rejected = _run(
        shell,
        script=instrumented,
        mode="Update",
        destination=destination,
        bundle=bundle_v2,
        environment=environment,
    )

    assert rejected.returncode != 0
    assert destination.is_dir()
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert sentinel.read_text(encoding="utf-8") == "must-survive"
    assert not (external_target / "NikaCore.exe").exists()
    assert not (external_target / "_internal").exists()
    rollback = destination.parent / f".{destination.name}.rollback"
    assert not rollback.exists(), "verified rollback image should have been restored"
