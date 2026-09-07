from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from nika_core.packaging.release import build_release_manifest, write_release_manifest

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_SCRIPT = ROOT / "scripts" / "install_nika_core.ps1"
SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


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


def _run(
    shell: str,
    script: Path,
    *,
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


def _instrument_post_activation_reparse(script_copy: Path) -> None:
    payload = PRODUCTION_SCRIPT.read_text(encoding="utf-8")
    needle = """            [System.IO.Directory]::Move($stagePath, $destinationPath)
            Assert-NikaNoReparsePathChain -Path $destinationPath
            Assert-NikaReleaseBundle -BundleRoot $destinationPath"""
    replacement = """            [System.IO.Directory]::Move($stagePath, $destinationPath)
            $qaTarget = [System.Environment]::GetEnvironmentVariable(
                "NIKA_QA_POST_ACTIVATION_REPARSE_TARGET"
            )
            if (-not [string]::IsNullOrWhiteSpace($qaTarget)) {
                [System.IO.Directory]::Delete($destinationPath, $true)
                New-Item -ItemType Junction -Path $destinationPath -Target $qaTarget | Out-Null
            }
            Assert-NikaNoReparsePathChain -Path $destinationPath
            Assert-NikaReleaseBundle -BundleRoot $destinationPath"""
    assert payload.count(needle) == 1, "exact update activation seam moved"
    script_copy.write_text(payload.replace(needle, replacement), encoding="utf-8")


@pytest.mark.skipif(os.name != "nt", reason="real post-activation junction proof is Windows-only")
def test_post_activation_junction_failure_restores_verified_old_image_without_touching_target(
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
        PRODUCTION_SCRIPT,
        mode="Install",
        destination=destination,
        bundle=bundle_v1,
    )
    assert installed.returncode == 0, installed.stderr or installed.stdout
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v1"

    attack_target = tmp_path / "НЕ ТОРКАТИСЯ"
    attack_target.mkdir()
    sentinel = attack_target / "sentinel.txt"
    sentinel.write_text("target-must-survive", encoding="utf-8")

    instrumented = tmp_path / "install_nika_core.post_activation_reparse_qa.ps1"
    _instrument_post_activation_reparse(instrumented)
    env = os.environ.copy()
    env["NIKA_QA_POST_ACTIVATION_REPARSE_TARGET"] = str(attack_target)

    rejected = _run(
        shell,
        instrumented,
        mode="Update",
        destination=destination,
        bundle=bundle_v2,
        env=env,
    )

    assert rejected.returncode != 0
    assert sentinel.read_text(encoding="utf-8") == "target-must-survive"
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert not (destination.parent / f".{destination.name}.rollback").exists()

    quarantined = list(destination.parent.glob(f".{destination.name}.failed-*"))
    assert len(quarantined) == 1
    assert sentinel.read_text(encoding="utf-8") == "target-must-survive"
