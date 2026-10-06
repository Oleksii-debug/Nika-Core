from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

from nika_core.packaging.release import (
    build_release_manifest,
    verify_release_archive,
    write_release_manifest,
)
from scripts import m12_release_evidence

SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"
TRUSTED_VERSION = "1.0.0"
MISMATCHED_VERSION = "999.0"


def _release_zip(tmp_path: Path, *, manifest_version: str) -> Path:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "NikaCore.exe").write_bytes(b"binary")
    manifest = build_release_manifest(
        bundle,
        product="NikaCore",
        version=manifest_version,
        source_sha=SOURCE_SHA,
    )
    write_release_manifest(bundle, manifest)
    artifact = tmp_path / "NikaCore-1.0.0-windows-x64.zip"
    with zipfile.ZipFile(artifact, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(bundle).as_posix())
    return artifact


def test_release_archive_rejects_embedded_version_mismatch(tmp_path: Path) -> None:
    artifact = _release_zip(tmp_path, manifest_version=MISMATCHED_VERSION)

    assert verify_release_archive(
        artifact,
        source_sha=SOURCE_SHA,
        expected_product_version=TRUSTED_VERSION,
    ) == ("archive:product-version",)


def test_m12_cli_propagates_trusted_version_to_archive_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "candidate.zip"
    evidence = tmp_path / "evidence.json"
    captured: dict[str, str | None] = {}

    monkeypatch.setattr(
        m12_release_evidence,
        "verify_distributable_evidence",
        lambda *args, **kwargs: (),
    )

    def _verify_archive(
        path: Path,
        *,
        source_sha: str,
        expected_product_version: str | None = None,
    ) -> tuple[str, ...]:
        assert path == artifact
        assert source_sha == SOURCE_SHA
        captured["product_version"] = expected_product_version
        return ()

    monkeypatch.setattr(m12_release_evidence, "verify_release_archive", _verify_archive)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "m12_release_evidence.py",
            "--artifact",
            str(artifact),
            "--evidence",
            str(evidence),
            "--source-sha",
            SOURCE_SHA,
            "--artifact-reference",
            "./dist/NikaCore-1.0.0-windows-x64.zip",
            "--product-version",
            TRUSTED_VERSION,
        ],
    )

    assert m12_release_evidence.main() == 0
    assert captured == {"product_version": TRUSTED_VERSION}
