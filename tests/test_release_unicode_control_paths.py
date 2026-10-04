from __future__ import annotations

import hashlib
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
INVALID_MARKS = ("\x85", "\u200e", "\u202e", "\u2028", "\u2029")


def _manifest(path: str) -> ReleaseManifest:
    return ReleaseManifest(
        product="NikaCore",
        version="1.0.0",
        source_sha=SOURCE_SHA,
        files=(
            ReleaseFile(
                path=path,
                size=4,
                sha256=hashlib.sha256(b"data").hexdigest(),
            ),
        ),
    )


@pytest.mark.parametrize("mark", INVALID_MARKS)
@pytest.mark.parametrize("position", ("basename", "directory"))
def test_manifest_rejects_invisible_path_components(
    tmp_path: Path, mark: str, position: str
) -> None:
    path = f"assets/{mark}state.txt" if position == "basename" else f"assets{mark}/state.txt"
    assert verify_release_manifest(tmp_path, _manifest(path)) == ("manifest:path:0",)


def test_manifest_rejects_unpaired_unicode_surrogate(tmp_path: Path) -> None:
    assert verify_release_manifest(tmp_path, _manifest("assets/\ud800.txt")) == (
        "manifest:path:0",
    )


@pytest.mark.parametrize("mark", INVALID_MARKS)
@pytest.mark.parametrize("position", ("basename", "directory"))
def test_archive_rejects_invisible_path_components_before_trusting_manifest(
    tmp_path: Path, mark: str, position: str
) -> None:
    path = f"assets/{mark}state.txt" if position == "basename" else f"assets{mark}/state.txt"
    artifact = tmp_path / "candidate.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("release-manifest.json", b"not trusted yet")
        archive.writestr("NikaCore.exe", b"binary")
        archive.writestr(path, b"data")
    assert verify_release_archive(artifact, source_sha=SOURCE_SHA) == (
        "archive:path:2",
    )


def test_ordinary_ukrainian_unicode_paths_remain_supported(tmp_path: Path) -> None:
    bundle = tmp_path / "NikaCore"
    bundle.mkdir()
    (bundle / "NikaCore.exe").write_bytes(b"binary")
    assets = bundle / "ресурси"
    assets.mkdir()
    (assets / "сповіщення.txt").write_text("Привіт, Ніко!\n", encoding="utf-8")
    manifest = build_release_manifest(
        bundle, product="NikaCore", version="1.0.0", source_sha=SOURCE_SHA
    )
    assert verify_release_manifest(bundle, manifest) == ()
    write_release_manifest(bundle, manifest)
    artifact = tmp_path / "valid.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(bundle).as_posix())
    assert verify_release_archive(artifact, source_sha=SOURCE_SHA) == ()
