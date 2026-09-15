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


def _install_then_two_updates(shell: str, tmp_path: Path) -> tuple[Path, Path, Path]:
    bundle_v1 = _bundle(tmp_path / "version-1", "v1")
    bundle_v2 = _bundle(tmp_path / "version-2", "v2")
    bundle_v3 = _bundle(tmp_path / "version-3", "v3")
    destination = tmp_path / "install" / "Nika Core"
    rollback = destination.parent / f".{destination.name}.rollback"

    installed = _run(shell, mode="Install", destination=destination, bundle=bundle_v1)
    assert installed.returncode == 0, installed.stderr or installed.stdout
    first_update = _run(shell, mode="Update", destination=destination, bundle=bundle_v2)
    assert first_update.returncode == 0, first_update.stderr or first_update.stdout
    second_update = _run(shell, mode="Update", destination=destination, bundle=bundle_v3)
    assert second_update.returncode == 0, second_update.stderr or second_update.stdout
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v3"
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v2"
    assert not list(destination.parent.glob(f".{destination.name}.first-update-*"))
    return destination, rollback, bundle_v3


def test_partial_retired_cleanup_uses_no_follow_tree_removal() -> None:
    payload = SCRIPT.read_text(encoding="utf-8")
    assert "function Remove-NikaTreeNoFollow" in payload
    recovery = payload[
        payload.index("function Resolve-NikaInterruptedUpdate") :
        payload.index("function Resolve-NikaInterruptedRollback")
    ]
    assert "Remove-NikaTreeNoFollow -Path $RetiredRollbackPath" in recovery
    assert "Remove-Item -LiteralPath $RetiredRollbackPath -Recurse -Force" not in payload


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_partial_retired_cleanup_after_committed_update_is_restart_safe(tmp_path: Path) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    destination, rollback, _ = _install_then_two_updates(shell, tmp_path)
    retired = destination.parent / f".{destination.name}.rollback-retired"

    # Model a hard interruption in recursive cleanup after a later update
    # committed: Destination and Rollback are authoritative, while Retired is
    # only installer-owned partially deleted residue and is not a valid bundle.
    retired.mkdir()
    (retired / "partial-delete.tmp").write_text("obsolete-fragment", encoding="utf-8")
    external = tmp_path / "external-sentinel"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("must-not-change", encoding="utf-8")

    resumed = _run(shell, mode="Rollback", destination=destination)
    assert resumed.returncode == 0, resumed.stderr or resumed.stdout
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v2"
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v3"
    assert not retired.exists()
    assert sentinel.read_text(encoding="utf-8") == "must-not-change"


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell filesystem proof is Windows-only")
def test_partial_retired_cleanup_rejects_nested_junction_without_touching_target(tmp_path: Path) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    destination, rollback, _ = _install_then_two_updates(shell, tmp_path)
    retired = destination.parent / f".{destination.name}.rollback-retired"
    retired.mkdir()
    (retired / "partial-delete.tmp").write_text("obsolete-fragment", encoding="utf-8")

    external = tmp_path / "external-retired-target"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("must-not-change", encoding="utf-8")
    nested = retired / "nested-junction"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(nested), str(external)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
        timeout=10,
    )
    assert created.returncode == 0, created.stderr or created.stdout

    failed = _run(shell, mode="Rollback", destination=destination)
    assert failed.returncode != 0, failed.stdout
    assert "nested reparse" in failed.stderr.lower()
    assert (destination / "NikaCore.exe").read_text(encoding="utf-8") == "v3"
    assert (rollback / "NikaCore.exe").read_text(encoding="utf-8") == "v2"
    assert retired.exists()
    assert sentinel.read_text(encoding="utf-8") == "must-not-change"

    removed = subprocess.run(
        ["cmd", "/c", "rmdir", str(nested)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
        timeout=10,
    )
    assert removed.returncode == 0, removed.stderr or removed.stdout
