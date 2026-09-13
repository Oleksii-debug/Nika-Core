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


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_complete_recovery_rejects_retired_junction_before_remove(tmp_path: Path) -> None:
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

    # Simulate the crash-left state handled by the hasDestination && hasRollback
    # recovery branch: both verified images are present and a valid retired image
    # is still waiting for safe cleanup.
    shutil.copytree(rollback, retired)
    assert (retired / "NikaCore.exe").read_text(encoding="utf-8") == "v1"

    external_target = tmp_path / "external-retired-target"
    external_target.mkdir()
    sentinel = external_target / "sentinel.txt"
    sentinel.write_text("must-not-change", encoding="utf-8")

    payload = SCRIPT.read_text(encoding="utf-8")
    needle = "    elseif ($hasDestination -and $hasRollback) {\n"
    assert payload.count(needle) == 1
    escaped_target = str(external_target).replace("'", "''")
    injected = needle + (
        "        $raceOriginal = $RetiredRollbackPath + '.race-original'\n"
        "        [System.IO.Directory]::Move($RetiredRollbackPath, $raceOriginal)\n"
        "        New-Item -ItemType Junction -Path $RetiredRollbackPath "
        f"-Target '{escaped_target}' | Out-Null\n"
        "        if ((Get-Item -LiteralPath $RetiredRollbackPath -Force).Attributes -band "
        "[System.IO.FileAttributes]::ReparsePoint) {\n"
        "            $null = $true\n"
        "        } else {\n"
        "            throw 'test retired junction injection did not create a reparse point'\n"
        "        }\n"
    )
    instrumented = tmp_path / "install_nika_core_remove_toctou_fault.ps1"
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
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v2"
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert (race_original / "NikaCore.exe").read_text(encoding="utf-8") == "v1"
    assert retired.exists(), "the guarded Remove-Item effect must not run"
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
