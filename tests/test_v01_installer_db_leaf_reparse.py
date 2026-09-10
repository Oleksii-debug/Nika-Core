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


def _bundle(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "NikaCore.exe").write_text("v1", encoding="utf-8")
    manifest = build_release_manifest(
        root,
        product="NikaCore",
        version="0.0.2",
        source_sha=SOURCE_SHA,
    )
    write_release_manifest(root, manifest)
    return root


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def test_installer_validates_canonical_database_leaf_before_parent_reduction() -> None:
    payload = SCRIPT.read_text(encoding="utf-8")
    canonical = "$canonicalDatabase = Get-NikaFullPath $databasePath"
    leaf_guard = "Assert-NikaNoReparsePathChain -Path $canonicalDatabase"
    parent = "$dataRoot = Split-Path -Parent $canonicalDatabase"
    assert canonical in payload
    assert leaf_guard in payload
    assert payload.index(canonical) < payload.index(leaf_guard) < payload.index(parent)


@pytest.mark.skipif(os.name != "nt", reason="real filesystem reparse proof is Windows-only")
def test_installer_rejects_configured_database_leaf_reparse_before_destination_mutation(
    tmp_path: Path,
) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    bundle = _bundle(tmp_path / "bundle")
    destination = tmp_path / "install" / "Nika Core"
    data_parent = tmp_path / "data"
    data_parent.mkdir()
    target = tmp_path / "database-target"
    target.mkdir()
    database_leaf = data_parent / "nika_core.db"

    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(database_leaf), str(target)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
        timeout=10,
    )
    if created.returncode != 0:
        pytest.skip("Windows runner does not permit leaf reparse creation")

    environment = os.environ.copy()
    environment["NIKA_DB_PATH"] = str(database_leaf)
    rejected = subprocess.run(
        [
            shell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            "-Mode",
            "Install",
            "-Destination",
            str(destination),
            "-BundlePath",
            str(bundle),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
        timeout=30,
        env=environment,
    )

    assert rejected.returncode != 0, rejected.stdout or rejected.stderr
    assert "Reparse points are forbidden in installer path authority" in rejected.stderr
    assert not destination.exists()
    assert not destination.parent.exists()
