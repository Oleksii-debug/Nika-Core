from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from nika_core.packaging.release import (
    ReleaseFile,
    ReleaseManifest,
    build_release_manifest,
    verify_release_manifest,
    write_release_manifest,
)

SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bundle(tmp_path: Path) -> tuple[Path, ReleaseFile]:
    bundle = tmp_path / "Nika Core"
    bundle.mkdir()
    executable = bundle / "NikaCore.exe"
    executable.write_bytes(b"binary")
    entry = ReleaseFile(
        path="NikaCore.exe",
        size=executable.stat().st_size,
        sha256=_sha256(executable),
    )
    return bundle, entry


def test_verifier_rejects_duplicate_conflicting_path_identity(tmp_path: Path) -> None:
    bundle, valid = _bundle(tmp_path)
    forged = ReleaseFile(path=valid.path, size=999, sha256="0" * 64)
    manifest = ReleaseManifest(
        product="NikaCore",
        version="1.0.0",
        source_sha=SOURCE_SHA,
        files=(forged, valid),
    )

    assert verify_release_manifest(bundle, manifest) == (
        "manifest:duplicate-path:NikaCore.exe",
    )


@pytest.mark.parametrize(
    ("manifest", "finding"),
    [
        (
            ReleaseManifest(
                product="NikaCore",
                version="1.0.0",
                source_sha=SOURCE_SHA,
                files=(ReleaseFile("a", 1, "0" * 64),),
                manifest_version=True,
            ),
            "manifest:schema-version",
        ),
        (
            ReleaseManifest(
                product=" NikaCore",
                version="1.0.0",
                source_sha=SOURCE_SHA,
                files=(ReleaseFile("a", 1, "0" * 64),),
            ),
            "manifest:product",
        ),
        (
            ReleaseManifest(
                product="NikaCore",
                version="1.0.0 ",
                source_sha=SOURCE_SHA,
                files=(ReleaseFile("a", 1, "0" * 64),),
            ),
            "manifest:product-version",
        ),
        (
            ReleaseManifest(
                product="NikaCore",
                version="1.0.0",
                source_sha="deadbeef",
                files=(ReleaseFile("a", 1, "0" * 64),),
            ),
            "manifest:source-sha",
        ),
    ],
)
def test_verifier_rejects_invalid_manifest_metadata(
    tmp_path: Path,
    manifest: ReleaseManifest,
    finding: str,
) -> None:
    bundle, _ = _bundle(tmp_path)
    assert finding in verify_release_manifest(bundle, manifest)


@pytest.mark.parametrize(
    "path",
    [
        "../outside.bin",
        "/absolute.bin",
        r"dir\file.bin",
        "dir//file.bin",
        "release-manifest.json",
        "C:/outside.bin",
    ],
)
def test_verifier_rejects_noncanonical_manifest_paths(tmp_path: Path, path: str) -> None:
    bundle, _ = _bundle(tmp_path)
    manifest = ReleaseManifest(
        product="NikaCore",
        version="1.0.0",
        source_sha=SOURCE_SHA,
        files=(ReleaseFile(path=path, size=1, sha256="0" * 64),),
    )
    assert verify_release_manifest(bundle, manifest) == ("manifest:path:0",)


@pytest.mark.parametrize(
    ("entry", "finding"),
    [
        (
            ReleaseFile(path="NikaCore.exe", size=True, sha256="0" * 64),
            "manifest:size-format:0",
        ),
        (
            ReleaseFile(path="NikaCore.exe", size=6, sha256="A" * 64),
            "manifest:sha256-format:0",
        ),
    ],
)
def test_verifier_rejects_invalid_file_evidence(
    tmp_path: Path,
    entry: ReleaseFile,
    finding: str,
) -> None:
    bundle, _ = _bundle(tmp_path)
    manifest = ReleaseManifest(
        product="NikaCore",
        version="1.0.0",
        source_sha=SOURCE_SHA,
        files=(entry,),
    )
    assert finding in verify_release_manifest(bundle, manifest)


def test_writer_refuses_malformed_manifest(tmp_path: Path) -> None:
    bundle, valid = _bundle(tmp_path)
    malformed = ReleaseManifest(
        product="NikaCore",
        version="1.0.0",
        source_sha=SOURCE_SHA,
        files=(valid, valid),
    )
    with pytest.raises(ValueError, match="duplicate-path"):
        write_release_manifest(bundle, malformed)
    assert not (bundle / "release-manifest.json").exists()


def test_builder_requires_exact_release_metadata(tmp_path: Path) -> None:
    bundle, _ = _bundle(tmp_path)
    with pytest.raises(ValueError, match="source-sha"):
        build_release_manifest(
            bundle,
            product="NikaCore",
            version="1.0.0",
            source_sha="deadbeef",
        )


def test_valid_manifest_still_verifies_and_writes(tmp_path: Path) -> None:
    bundle, _ = _bundle(tmp_path)
    manifest = build_release_manifest(
        bundle,
        product="NikaCore",
        version="1.0.0",
        source_sha=SOURCE_SHA,
    )
    assert verify_release_manifest(bundle, manifest) == ()
    target = write_release_manifest(bundle, manifest)
    assert target.is_file()
