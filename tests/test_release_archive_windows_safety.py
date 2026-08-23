from __future__ import annotations

import stat
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


@pytest.mark.parametrize(
    "path",
    [
        "CON",
        "NUL.txt",
        "bad?.dll",
        "bad*.dll",
        "bad<name>.dll",
        "bad|name.dll",
        "trailing.",
        "trailing ",
    ],
)
def test_manifest_rejects_windows_unsafe_path_identity(tmp_path: Path, path: str) -> None:
    bundle = tmp_path / "Nika Core"
    bundle.mkdir()
    manifest = ReleaseManifest(
        product="NikaCore",
        version="1.0.0",
        source_sha=SOURCE_SHA,
        files=(ReleaseFile(path=path, size=1, sha256="0" * 64),),
    )

    assert "manifest:path:0" in verify_release_manifest(bundle, manifest)


def test_manifest_rejects_windows_casefold_collision(tmp_path: Path) -> None:
    bundle = tmp_path / "Nika Core"
    bundle.mkdir()
    manifest = ReleaseManifest(
        product="NikaCore",
        version="1.0.0",
        source_sha=SOURCE_SHA,
        files=(
            ReleaseFile(path="Bin/Nika.dll", size=1, sha256="0" * 64),
            ReleaseFile(path="bin/NIKA.DLL", size=1, sha256="1" * 64),
        ),
    )

    assert "manifest:windows-path-collision:bin/NIKA.DLL" in verify_release_manifest(
        bundle,
        manifest,
    )


def test_release_archive_rejects_windows_casefold_collision(tmp_path: Path) -> None:
    artifact = tmp_path / "collision.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("release-manifest.json", b"{}")
        archive.writestr("Bin/Nika.dll", b"a")
        archive.writestr("bin/NIKA.DLL", b"b")

    assert verify_release_archive(artifact, source_sha=SOURCE_SHA) == (
        "archive:windows-path-collision:bin/NIKA.DLL",
    )


def test_release_archive_rejects_symlink_member_metadata(tmp_path: Path) -> None:
    artifact = tmp_path / "symlink.zip"
    symlink = zipfile.ZipInfo("link.dll")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("release-manifest.json", b"{}")
        archive.writestr(symlink, b"../outside.dll")

    assert verify_release_archive(artifact, source_sha=SOURCE_SHA) == ("archive:symlink:1",)
