from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from nika_core.packaging.release import (
    ReleaseFile,
    ReleaseManifest,
    verify_release_archive,
    verify_release_manifest,
)

SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"
PAYLOAD = b"data"
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


def _manifest(path: str) -> ReleaseManifest:
    return ReleaseManifest(
        product="NikaCore",
        version="1.0.0",
        source_sha=SOURCE_SHA,
        files=(ReleaseFile(path=path, size=len(PAYLOAD), sha256=DIGEST),),
    )


def _archive(path: Path, member_path: str) -> None:
    manifest = _manifest(member_path)
    body = {
        "manifest_version": manifest.manifest_version,
        "product": manifest.product,
        "version": manifest.version,
        "source_sha": manifest.source_sha,
        "files": [
            {"path": member.path, "size": member.size, "sha256": member.sha256}
            for member in manifest.files
        ],
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("release-manifest.json", json.dumps(body))
        archive.writestr(member_path, PAYLOAD)


@pytest.mark.parametrize(
    "path",
    (
        "x" * 256,
        "📦" * 128,
        "📦" * 127 + "ab",
        "assets/" + "x" * 256 + "/state.txt",
        "assets/" + "📦" * 128 + "/state.txt",
    ),
)
def test_manifest_rejects_oversized_ntfs_component(tmp_path: Path, path: str) -> None:
    assert verify_release_manifest(tmp_path, _manifest(path)) == ("manifest:path:0",)


@pytest.mark.parametrize(
    "path",
    (
        "x" * 256,
        "📦" * 128,
        "📦" * 127 + "ab",
        "assets/" + "x" * 256 + "/state.txt",
        "assets/" + "📦" * 128 + "/state.txt",
    ),
)
def test_archive_rejects_oversized_component_before_manifest(
    tmp_path: Path, path: str
) -> None:
    artifact = tmp_path / "oversized.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("release-manifest.json", b"untrusted")
        archive.writestr(path, PAYLOAD)
    assert verify_release_archive(artifact, source_sha=SOURCE_SHA) == ("archive:path:1",)


@pytest.mark.parametrize(
    "path",
    (
        "x" * 255,
        "📦" * 127 + "a",
        "ресурси/сповіщення.txt",
    ),
)
def test_valid_component_length_preserves_manifest_zip_round_trip(
    tmp_path: Path, path: str
) -> None:
    assert verify_release_manifest(tmp_path, _manifest(path)) == ()
    artifact = tmp_path / "valid.zip"
    _archive(artifact, path)
    assert verify_release_archive(artifact, source_sha=SOURCE_SHA) == ()
