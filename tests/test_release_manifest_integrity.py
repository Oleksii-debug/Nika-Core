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


def _write_release_zip(bundle: Path, target: Path) -> None:
    import zipfile

    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(bundle).as_posix())


def _write_outer_evidence(evidence: Path, artifact: Path) -> None:
    import json

    evidence.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "commit_sha": SOURCE_SHA,
                "distributable_zip_path": "./dist/NikaCore-1.0.0-windows-x64.zip",
                "distributable_zip_sha256": _sha256(artifact),
                "distributable_zip_size": artifact.stat().st_size,
            }
        ),
        encoding="utf-8",
    )


def _valid_release_zip(tmp_path: Path) -> tuple[Path, Path]:
    bundle, _ = _bundle(tmp_path)
    manifest = build_release_manifest(
        bundle,
        product="NikaCore",
        version="1.0.0",
        source_sha=SOURCE_SHA,
    )
    write_release_manifest(bundle, manifest)
    artifact = tmp_path / "NikaCore-1.0.0-windows-x64.zip"
    _write_release_zip(bundle, artifact)
    return bundle, artifact


def test_release_archive_verifies_embedded_manifest(tmp_path: Path) -> None:
    from nika_core.packaging.release import verify_release_archive

    _, artifact = _valid_release_zip(tmp_path)
    assert verify_release_archive(artifact, source_sha=SOURCE_SHA) == ()


def test_release_archive_rejects_post_manifest_payload_tamper(tmp_path: Path) -> None:
    from nika_core.packaging.release import verify_release_archive

    bundle, _ = _valid_release_zip(tmp_path)
    (bundle / "NikaCore.exe").write_bytes(b"tampered-after-manifest-verification")
    artifact = tmp_path / "tampered.zip"
    _write_release_zip(bundle, artifact)
    assert verify_release_archive(artifact, source_sha=SOURCE_SHA) == (
        "archive:size:NikaCore.exe",
    )


def test_release_archive_rejects_missing_manifest(tmp_path: Path) -> None:
    import zipfile

    artifact = tmp_path / "missing-manifest.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("NikaCore.exe", b"binary")
    from nika_core.packaging.release import verify_release_archive

    assert verify_release_archive(artifact, source_sha=SOURCE_SHA) == (
        "archive:missing-manifest",
    )


def test_release_archive_rejects_duplicate_member_identity(tmp_path: Path) -> None:
    import warnings
    import zipfile

    bundle, _ = _valid_release_zip(tmp_path)
    manifest_content = (bundle / "release-manifest.json").read_bytes()
    artifact = tmp_path / "duplicate.zip"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(artifact, "w") as archive:
            archive.writestr("release-manifest.json", manifest_content)
            archive.writestr("NikaCore.exe", b"binary")
            archive.writestr("NikaCore.exe", b"binary")
    from nika_core.packaging.release import verify_release_archive

    assert verify_release_archive(artifact, source_sha=SOURCE_SHA) == (
        "archive:duplicate-path:NikaCore.exe",
    )


def test_release_archive_rejects_traversal_member(tmp_path: Path) -> None:
    import zipfile

    bundle, _ = _valid_release_zip(tmp_path)
    manifest_content = (bundle / "release-manifest.json").read_bytes()
    artifact = tmp_path / "traversal.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("release-manifest.json", manifest_content)
        archive.writestr("NikaCore.exe", b"binary")
        archive.writestr("../escape.dll", b"escape")
    from nika_core.packaging.release import verify_release_archive

    assert verify_release_archive(artifact, source_sha=SOURCE_SHA) == ("archive:path:2",)


def test_m12_cli_rejects_outer_bound_zip_with_inner_manifest_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    from scripts import m12_release_evidence

    bundle, _ = _valid_release_zip(tmp_path)
    (bundle / "NikaCore.exe").write_bytes(b"tampered-after-manifest-verification")
    artifact = tmp_path / "NikaCore-1.0.0-windows-x64.zip"
    _write_release_zip(bundle, artifact)
    evidence = tmp_path / "evidence.json"
    _write_outer_evidence(evidence, artifact)
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
        ],
    )
    with pytest.raises(SystemExit, match="archive:size:NikaCore.exe"):
        m12_release_evidence.main()


def test_outer_evidence_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    from nika_core.packaging.release import verify_distributable_evidence

    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"candidate")
    evidence = tmp_path / "evidence.json"
    evidence.write_text(
        '{"commit_sha":"ffffffffffffffffffffffffffffffffffffffff",'
        f'"commit_sha":"{SOURCE_SHA}",'
        '"distributable_zip_path":"ref",'
        f'"distributable_zip_size":{artifact.stat().st_size},'
        f'"distributable_zip_sha256":"{_sha256(artifact)}"}}',
        encoding="utf-8",
    )
    assert verify_distributable_evidence(
        artifact,
        evidence,
        source_sha=SOURCE_SHA,
        artifact_reference="ref",
    ) == ("distributable:invalid-evidence",)
