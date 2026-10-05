from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from nika_core.packaging.release import (
    ReleaseFile,
    ReleaseManifest,
    build_release_manifest,
    verify_release_archive,
    verify_release_manifest,
    write_release_manifest,
)

SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"


def _file(path: str, content: bytes) -> ReleaseFile:
    return ReleaseFile(
        path=path, size=len(content), sha256=hashlib.sha256(content).hexdigest()
    )


def _manifest(*files: ReleaseFile) -> ReleaseManifest:
    return ReleaseManifest(
        product="NikaCore",
        version="1.0.0",
        source_sha=SOURCE_SHA,
        files=files,
    )


def _archive(path: Path, *files: tuple[str, bytes], dirs: tuple[str, ...] = ()) -> None:
    manifest = _manifest(*(_file(name, data) for name, data in files))
    payload = {
        "manifest_version": manifest.manifest_version,
        "product": manifest.product,
        "version": manifest.version,
        "source_sha": manifest.source_sha,
        "files": [
            {"path": entry.path, "size": entry.size, "sha256": entry.sha256}
            for entry in manifest.files
        ],
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("release-manifest.json", json.dumps(payload))
        for directory in dirs:
            archive.writestr(directory, b"")
        for name, data in files:
            archive.writestr(name, data)


@pytest.mark.parametrize("parent", ["bin", "Bin"])
def test_manifest_rejects_file_used_as_casefolded_directory(
    tmp_path: Path, parent: str
) -> None:
    manifest = _manifest(
        _file(parent, b"not a directory"),
        _file("bin/plugins/driver.dll", b"driver"),
    )
    assert verify_release_manifest(tmp_path, manifest) == (
        "manifest:file-directory-collision:bin/plugins/driver.dll",
    )


def test_manifest_reserves_its_own_file_identity_as_non_directory(
    tmp_path: Path,
) -> None:
    manifest = _manifest(_file("release-manifest.json/payload", b"payload"))
    assert verify_release_manifest(tmp_path, manifest) == (
        "manifest:file-directory-collision:release-manifest.json/payload",
    )


@pytest.mark.parametrize("parent", ["bin", "Bin"])
def test_archive_rejects_manifest_bound_file_directory_collision(
    tmp_path: Path, parent: str
) -> None:
    artifact = tmp_path / "collision.zip"
    _archive(
        artifact,
        (parent, b"not a directory"),
        ("bin/plugins/driver.dll", b"driver"),
    )
    assert verify_release_archive(artifact, source_sha=SOURCE_SHA) == (
        "archive:file-directory-collision:bin/plugins/driver.dll",
    )


def test_archive_rejects_implicit_parent_file_of_explicit_directory(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "collision.zip"
    _archive(artifact, ("bin", b"not a directory"), dirs=("bin/plugins/",))
    assert verify_release_archive(artifact, source_sha=SOURCE_SHA) == (
        "archive:file-directory-collision:bin/plugins",
    )


def test_archive_rejects_manifest_file_as_a_directory(tmp_path: Path) -> None:
    artifact = tmp_path / "manifest-as-directory.zip"
    _archive(artifact, ("release-manifest.json/payload", b"payload"))
    assert verify_release_archive(artifact, source_sha=SOURCE_SHA) == (
        "archive:file-directory-collision:release-manifest.json/payload",
    )


def test_archive_allows_normal_nested_files_and_directory_entries(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "normal.zip"
    _archive(
        artifact,
        ("bin/NikaCore.exe", b"exe"),
        ("bin/plugins/driver.dll", b"driver"),
        dirs=("bin/", "bin/plugins/"),
    )
    assert verify_release_archive(artifact, source_sha=SOURCE_SHA) == ()


@pytest.mark.parametrize("alias", ["Release-Manifest.json", "RELEASE-MANIFEST.JSON"])
def test_manifest_rejects_casefold_alias_of_reserved_root_file(
    tmp_path: Path, alias: str
) -> None:
    assert verify_release_manifest(tmp_path, _manifest(_file(alias, b"alias"))) == (
        "manifest:path:0",
    )


def test_nested_manifest_named_asset_is_manifest_bound(tmp_path: Path) -> None:
    bundle = tmp_path / "Nika Core"
    (bundle / "assets").mkdir(parents=True)
    (bundle / "NikaCore.exe").write_bytes(b"exe")
    nested = bundle / "assets" / "release-manifest.json"
    nested.write_bytes(b"nested")

    manifest = build_release_manifest(
        bundle, product="NikaCore", version="1.0.0", source_sha=SOURCE_SHA
    )
    assert {item.path for item in manifest.files} == {
        "NikaCore.exe", "assets/release-manifest.json"
    }
    write_release_manifest(bundle, manifest)
    assert verify_release_manifest(bundle, manifest) == ()

    nested.write_bytes(b"modified")
    assert verify_release_manifest(bundle, manifest) == (
        "size:assets/release-manifest.json",
    )
