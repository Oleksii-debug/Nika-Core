from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from nika_core.packaging.release import build_release_manifest, write_release_manifest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install_nika_core.ps1"
SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def _bundle(root: Path) -> Path:
    bundle = root / "bundle"
    bundle.mkdir(parents=True)
    (bundle / "NikaCore.exe").write_text("fixture", encoding="utf-8")
    manifest = build_release_manifest(
        bundle,
        product="NikaCore",
        version="0.0.2",
        source_sha=SOURCE_SHA,
    )
    write_release_manifest(bundle, manifest)
    return bundle


def _run_install(shell: str, *, bundle: Path, destination: Path, data_root: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["NIKA_DB_PATH"] = str(data_root / "nika_core.db")
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


def _duplicate_top_level(raw: str) -> str:
    marker = '  "product": "NikaCore",'
    assert raw.count(marker) == 1
    return raw.replace(marker, '  "product": "tampered",\n' + marker, 1)


def _duplicate_file_member(raw: str) -> str:
    marker = '      "path": "NikaCore.exe",'
    assert raw.count(marker) == 1
    return raw.replace(marker, '      "path": "ignored.exe",\n' + marker, 1)


def _rewrite_object(raw: str, mutate: Callable[[dict[str, object]], None]) -> str:
    payload = json.loads(raw)
    mutate(payload)
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def test_installer_manifest_ingress_declares_strict_object_authority() -> None:
    payload = SCRIPT.read_text(encoding="utf-8")
    assert "function Assert-NikaUniqueJsonObjectKeys" in payload
    assert "function Assert-NikaExactJsonObjectShape" in payload
    assert "Release manifest contains a duplicate JSON member" in payload
    assert "Release manifest object shape is invalid" in payload
    assert "[System.StringComparer]::Ordinal.Equals" in payload


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell manifest proof is Windows-only")
@pytest.mark.parametrize("mutate_raw", [_duplicate_top_level, _duplicate_file_member])
def test_duplicate_json_members_fail_before_install_mutation(
    tmp_path: Path,
    mutate_raw: Callable[[str], str],
) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    bundle = _bundle(tmp_path / "case")
    manifest_path = bundle / "release-manifest.json"
    manifest_path.write_text(mutate_raw(manifest_path.read_text(encoding="utf-8")), encoding="utf-8")
    destination = tmp_path / "install" / "Nika Core"

    rejected = _run_install(
        shell,
        bundle=bundle,
        destination=destination,
        data_root=tmp_path / "data",
    )

    assert rejected.returncode != 0, rejected.stdout or rejected.stderr
    assert "duplicate JSON member" in rejected.stderr
    assert not destination.exists()


def _case_variant(payload: dict[str, object]) -> None:
    payload["product"] = "nikacore"


def _array_product(payload: dict[str, object]) -> None:
    payload["product"] = ["NikaCore"]


def _float_manifest_version(payload: dict[str, object]) -> None:
    payload["manifest_version"] = 2.0


def _extra_top_level(payload: dict[str, object]) -> None:
    payload["extra_authority"] = "ignored-before-repair"


def _missing_version(payload: dict[str, object]) -> None:
    payload.pop("version")


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell manifest proof is Windows-only")
@pytest.mark.parametrize(
    "mutate",
    [_case_variant, _array_product, _float_manifest_version, _extra_top_level, _missing_version],
)
def test_noncanonical_manifest_shape_fails_before_install_mutation(
    tmp_path: Path,
    mutate: Callable[[dict[str, object]], None],
) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    bundle = _bundle(tmp_path / "case")
    manifest_path = bundle / "release-manifest.json"
    raw = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text(_rewrite_object(raw, mutate), encoding="utf-8")
    destination = tmp_path / "install" / "Nika Core"

    rejected = _run_install(
        shell,
        bundle=bundle,
        destination=destination,
        data_root=tmp_path / "data",
    )

    assert rejected.returncode != 0, rejected.stdout or rejected.stderr
    assert not destination.exists()
