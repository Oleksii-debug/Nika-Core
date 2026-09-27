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
    (internal / "runtime.dat").write_text(
        f"runtime-{marker}",
        encoding="utf-8",
    )
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


def test_interrupted_cleanup_revalidates_retired_before_no_follow_remove() -> None:
    payload = SCRIPT.read_text(encoding="utf-8")
    recovery = payload[
        payload.index("function Resolve-NikaInterruptedUpdate") :
        payload.index("function Resolve-NikaInterruptedRollback")
    ]
    branch = recovery[
        recovery.index("elseif ($hasDestination -and $hasRollback) {") :
        recovery.index(
            'throw "Interrupted update state cannot be restored without losing a '
            'verified image."'
        )
    ]
    remove = "Remove-NikaTreeNoFollow -Path $RetiredRollbackPath"
    reparse = "Assert-NikaNoReparsePathChain -Path $RetiredRollbackPath"
    separation = "Assert-NikaDataMutationSeparation -DataRoot $DataRoot -MutationPaths @("

    assert branch.index(reparse) < branch.index(remove)
    assert branch.index(separation) < branch.index(remove)


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_interrupted_cleanup_rejects_retired_junction_before_remove(tmp_path: Path) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    bundle_v1 = _bundle(tmp_path / "version-1", "v1")
    bundle_v2 = _bundle(tmp_path / "version-2", "v2")
    bundle_v3 = _bundle(tmp_path / "version-3", "v3")
    bundle_v4 = _bundle(tmp_path / "version-4", "v4")
    destination = tmp_path / "install" / "Nika Core"
    rollback = destination.parent / f".{destination.name}.rollback"
    retired = destination.parent / f".{destination.name}.rollback-retired"

    installed = _run(shell, script=SCRIPT, mode="Install", destination=destination, bundle=bundle_v1)
    assert installed.returncode == 0, installed.stderr or installed.stdout
    first_update = _run(shell, script=SCRIPT, mode="Update", destination=destination, bundle=bundle_v2)
    assert first_update.returncode == 0, first_update.stderr or first_update.stdout
    second_update = _run(shell, script=SCRIPT, mode="Update", destination=destination, bundle=bundle_v3)
    assert second_update.returncode == 0, second_update.stderr or second_update.stdout
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v3"
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v2"

    shutil.copytree(rollback, retired)
    assert (retired / "NikaCore.exe").read_text(encoding="utf-8") == "v2"

    external_target = tmp_path / "external-retired-cleanup-target"
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
        "        $raceRetired = Get-Item -LiteralPath $RetiredRollbackPath -Force\n"
        "        if (($raceRetired.Attributes -band "
        "[System.IO.FileAttributes]::ReparsePoint) -eq 0) {\n"
        "            throw 'test retired cleanup junction injection failed'\n"
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
        bundle=bundle_v4,
    )
    assert failed.returncode != 0, failed.stdout
    assert "Reparse points are forbidden" in failed.stderr
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v3"
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v2"
    assert (race_original / "NikaCore.exe").read_text(encoding="utf-8") == "v2"
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
