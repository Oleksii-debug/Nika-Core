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
INVALID_MARKS = ("\x7f", "\x85", "\u200e", "\u202e", "\u2028", "\u2029")


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


@pytest.mark.parametrize("field", ("product", "version"))
@pytest.mark.parametrize("mark", ("\x7f", "\u202e", "\u2028", "\u2029", "\ud800"))
def test_manifest_rejects_spoofed_release_identity_labels(
    tmp_path: Path, field: str, mark: str
) -> None:
    original = _manifest("NikaCore.exe")
    fields = {
        "product": original.product,
        "version": original.version,
        "source_sha": original.source_sha,
        "files": original.files,
    }
    fields[field] = f"safe{mark}label"
    manifest = ReleaseManifest(**fields)
    finding = "manifest:product" if field == "product" else "manifest:product-version"
    assert verify_release_manifest(tmp_path, manifest) == (finding,)


@pytest.mark.parametrize("field", ("product", "version"))
def test_archive_rejects_controlled_release_identity_labels(
    tmp_path: Path, field: str
) -> None:
    manifest = _manifest("NikaCore.exe")
    payload = {
        "manifest_version": manifest.manifest_version,
        "product": manifest.product,
        "version": manifest.version,
        "source_sha": manifest.source_sha,
        "files": [
            {"path": item.path, "size": item.size, "sha256": item.sha256}
            for item in manifest.files
        ],
    }
    payload[field] = "safe\u202elabel"
    artifact = tmp_path / "spoofed.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("release-manifest.json", json.dumps(payload))
        archive.writestr("NikaCore.exe", b"data")
    finding = "manifest:product" if field == "product" else "manifest:product-version"
    assert verify_release_archive(artifact, source_sha=SOURCE_SHA) == (
        f"archive:{finding}",
    )


def test_manifest_rejects_overlong_product_version(tmp_path: Path) -> None:
    original = _manifest("NikaCore.exe")
    manifest = ReleaseManifest(
        product=original.product,
        version="v" * 129,
        source_sha=SOURCE_SHA,
        files=original.files,
    )
    assert verify_release_manifest(tmp_path, manifest) == ("manifest:product-version",)


def test_unicode_product_label_remains_valid(tmp_path: Path) -> None:
    (tmp_path / "NikaCore.exe").write_bytes(b"data")
    original = _manifest("NikaCore.exe")
    manifest = ReleaseManifest(
        product="Ніка Кор",
        version="2.0-beta",
        source_sha=SOURCE_SHA,
        files=original.files,
    )
    assert verify_release_manifest(tmp_path, manifest) == ()
