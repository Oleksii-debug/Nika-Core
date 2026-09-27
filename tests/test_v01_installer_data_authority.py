from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install_nika_core.ps1"


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def _run_preflight(
    shell: str,
    *,
    destination: Path,
    env_overrides: dict[str, str],
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("NIKA_DB_PATH", None)
    environment.pop("NIKA_DATABASE_PATH", None)
    environment.update(env_overrides)
    return subprocess.run(
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
            str(destination.parent / "missing-bundle"),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
        timeout=30,
        env=environment,
        cwd=cwd,
    )


def test_installer_contract_requires_fully_qualified_data_authority() -> None:
    payload = SCRIPT.read_text(encoding="utf-8")
    assert "function Test-NikaFullyQualifiedWindowsPath" in payload
    assert "Configured Nika Core database path must be fully qualified." in payload
    assert "LOCALAPPDATA must be a fully qualified local or UNC path." in payload
    assert "[System.IO.Path]::IsPathRooted($databasePath)" not in payload


def test_installer_root_identity_helpers_share_one_absolute_representation() -> None:
    payload = SCRIPT.read_text(encoding="utf-8")

    assert "return [System.IO.Path]::GetFullPath($Path).TrimEnd(" not in payload
    assert "$trimmedFullPath = $fullPath.TrimEnd(" in payload
    assert "$trimmedRoot = $root.TrimEnd(" in payload
    assert "return $root" in payload
    assert "$rootPrefix = $Root" in payload
    assert "$Path.StartsWith($rootPrefix" in payload
    assert (
        "$volumeRoot = Get-NikaFullPath ([System.IO.Path]::GetPathRoot($fullPath))" in payload
    )
    assert (
        "$root = Get-NikaFullPath ([System.IO.Path]::GetPathRoot($DestinationPath))" in payload
    )
    assert "Equals($DestinationPath, $root)" in payload


@pytest.mark.skipif(os.name != "nt", reason="real Windows path semantics are Windows-only")
@pytest.mark.parametrize("variable", ["NIKA_DB_PATH", "NIKA_DATABASE_PATH"])
@pytest.mark.parametrize("kind", ["drive-relative", "bare-drive", "root-relative"])
def test_configured_database_authority_rejects_non_fully_qualified_paths_before_mutation(
    tmp_path: Path,
    variable: str,
    kind: str,
) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    drive = tmp_path.drive or "C:"
    configured = {
        "drive-relative": f"{drive}relative\\nika_core.db",
        "bare-drive": drive,
        "root-relative": r"\nika-data\nika_core.db",
    }[kind]
    destination = tmp_path / "install-root" / "Nika Core"

    rejected = _run_preflight(
        shell,
        destination=destination,
        env_overrides={variable: configured},
    )

    assert rejected.returncode != 0, rejected.stdout or rejected.stderr
    assert "Configured Nika Core database path must be fully qualified." in rejected.stderr
    assert not destination.parent.exists()


@pytest.mark.skipif(os.name != "nt", reason="real Windows path semantics are Windows-only")
@pytest.mark.parametrize("variable", ["NIKA_DB_PATH", "NIKA_DATABASE_PATH"])
def test_root_level_database_keeps_whole_drive_data_authority(
    tmp_path: Path,
    variable: str,
) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")
    if not tmp_path.drive:
        pytest.skip("temporary directory has no Windows drive identity")

    working_directory = tmp_path / "independent-cwd"
    working_directory.mkdir()
    destination = tmp_path / "install-root" / "Nika Core"
    configured = f"{tmp_path.drive}\\nika_core.db"

    rejected = _run_preflight(
        shell,
        destination=destination,
        env_overrides={variable: configured},
        cwd=working_directory,
    )

    assert rejected.returncode != 0, rejected.stdout or rejected.stderr
    assert "Installer mutation path must not overlap the canonical Nika Core data root." in (
        rejected.stderr
    )
    assert "BundlePath does not exist." not in rejected.stderr
    assert not destination.parent.exists()


@pytest.mark.skipif(os.name != "nt", reason="real Windows path semantics are Windows-only")
@pytest.mark.parametrize("variable", ["NIKA_DB_PATH", "NIKA_DATABASE_PATH"])
def test_unicode_space_absolute_database_authority_passes_path_preflight(
    tmp_path: Path,
    variable: str,
) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    configured = tmp_path / "Дані користувача" / "простір даних" / "nika_core.db"
    destination = tmp_path / "install-root" / "Nika Core"
    reached_bundle_validation = _run_preflight(
        shell,
        destination=destination,
        env_overrides={variable: str(configured)},
    )

    assert reached_bundle_validation.returncode != 0
    assert "must be fully qualified" not in reached_bundle_validation.stderr
    assert "BundlePath does not exist." in reached_bundle_validation.stderr
    assert not destination.parent.exists()


@pytest.mark.skipif(os.name != "nt", reason="real Windows path semantics are Windows-only")
def test_relative_localappdata_is_rejected_before_default_data_authority_can_use_cwd(
    tmp_path: Path,
) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    destination = tmp_path / "install-root" / "Nika Core"
    rejected = _run_preflight(
        shell,
        destination=destination,
        env_overrides={"LOCALAPPDATA": "relative-local-app-data"},
    )

    assert rejected.returncode != 0, rejected.stdout or rejected.stderr
    assert "LOCALAPPDATA must be a fully qualified local or UNC path." in rejected.stderr
    assert not destination.parent.exists()
