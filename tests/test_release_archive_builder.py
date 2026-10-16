from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from nika_core.packaging.release import (
    build_release_archive,
    build_release_manifest,
    verify_release_archive,
    write_release_manifest,
)

SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"
VERSION = "1.0.0"


def _bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "Nika Core — випуск"
    bundle.mkdir()
    (bundle / "NikaCore.exe").write_bytes(b"windows executable")
    (bundle / ".hidden-resource.bin").write_bytes(b"required hidden asset")
    nested = bundle / "ресурси" / "сповіщення"
    nested.mkdir(parents=True)
    (nested / "повідомлення.txt").write_text("Привіт", encoding="utf-8")
    manifest = build_release_manifest(
        bundle, product="NikaCore", version=VERSION, source_sha=SOURCE_SHA
    )
    write_release_manifest(bundle, manifest)
    return bundle


def _publish(bundle: Path, artifact: Path) -> Path:
    return build_release_archive(
        bundle,
        artifact,
        source_sha=SOURCE_SHA,
        expected_product_version=VERSION,
    )


def test_final_zip_includes_hidden_assets_ukrainian_paths_and_manifest(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    output = tmp_path / "готовий пакет"
    output.mkdir()
    artifact = output / "Ніка Core 1.0.zip"
    assert _publish(bundle, artifact) == artifact
    with zipfile.ZipFile(artifact) as archive:
        members = set(archive.namelist())
        assert members == {
            "NikaCore.exe",
            ".hidden-resource.bin",
            "ресурси/сповіщення/повідомлення.txt",
            "release-manifest.json",
        }
        assert archive.read(".hidden-resource.bin") == b"required hidden asset"
    assert verify_release_archive(
        artifact, source_sha=SOURCE_SHA, expected_product_version=VERSION
    ) == ()
    assert not list(output.glob(".nika-release-*"))


@pytest.mark.skipif(os.name != "nt", reason="Windows native hidden attribute")
def test_final_zip_includes_windows_hidden_attribute_file(tmp_path: Path) -> None:
    import ctypes

    bundle = _bundle(tmp_path)
    hidden = bundle / ".hidden-resource.bin"
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
    kernel.GetFileAttributesW.restype = ctypes.c_uint32
    kernel.SetFileAttributesW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    kernel.SetFileAttributesW.restype = ctypes.c_int
    flags = kernel.GetFileAttributesW(str(hidden))
    assert flags != 0xFFFFFFFF
    assert kernel.SetFileAttributesW(str(hidden), flags | 0x02) != 0
    artifact = tmp_path / "Windows hidden.zip"
    _publish(bundle, artifact)
    with zipfile.ZipFile(artifact) as archive:
        assert archive.read(".hidden-resource.bin") == b"required hidden asset"


def test_rejects_modified_bundle_without_replacing_existing_zip(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    artifact = tmp_path / "release.zip"
    artifact.write_bytes(b"previous approved artifact")
    (bundle / "NikaCore.exe").write_bytes(b"tampered executable")

    with pytest.raises(ValueError, match="release bundle verification failed"):
        _publish(bundle, artifact)
    assert artifact.read_bytes() == b"previous approved artifact"
    assert not list(tmp_path.glob(".nika-release-*"))


def test_rejects_broken_zip_after_creation_without_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nika_core.packaging import release

    bundle = _bundle(tmp_path)
    artifact = tmp_path / "release.zip"
    artifact.write_bytes(b"previous approved artifact")
    monkeypatch.setattr(
        release,
        "verify_release_archive",
        lambda *args, **kwargs: ("archive:unreadable:NikaCore.exe",),
    )
    with pytest.raises(ValueError, match="release ZIP verification failed"):
        _publish(bundle, artifact)
    assert artifact.read_bytes() == b"previous approved artifact"
    assert not list(tmp_path.glob(".nika-release-*"))


def test_rejects_mismatched_manifest_identity(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    with pytest.raises(ValueError, match="manifest does not match"):
        build_release_archive(
            bundle,
            tmp_path / "release.zip",
            source_sha="f" * 40,
            expected_product_version=VERSION,
        )
    with pytest.raises(ValueError, match="manifest does not match"):
        build_release_archive(
            bundle,
            tmp_path / "release.zip",
            source_sha=SOURCE_SHA,
            expected_product_version="2.0.0",
        )


def test_rejects_missing_manifest_and_output_inside_bundle(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    with pytest.raises(ValueError, match="outside its input bundle"):
        _publish(bundle, bundle / "self-inclusion.zip")
    (bundle / "release-manifest.json").unlink()
    with pytest.raises(ValueError, match="missing its regular manifest"):
        _publish(bundle, tmp_path / "release.zip")
    assert not (tmp_path / "release.zip").exists()


def test_rejects_malformed_source_sha_before_publication(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    with pytest.raises(ValueError, match="exact source SHA"):
        build_release_archive(
            bundle,
            tmp_path / "release.zip",
            source_sha="invalid",
            expected_product_version=VERSION,
        )


def test_rejects_oversized_manifest_before_reading_or_publishing(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    (bundle / "release-manifest.json").write_bytes(b" " * (4 * 1024 * 1024 + 1))
    artifact = tmp_path / "previous.zip"
    artifact.write_bytes(b"existing")
    with pytest.raises(ValueError, match="manifest exceeds the maximum size"):
        _publish(bundle, artifact)
    assert artifact.read_bytes() == b"existing"
    assert not list(tmp_path.glob(".nika-release-*"))


def test_directory_symlink_cannot_silently_omit_bundle_assets(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    alias = bundle / "alias"
    try:
        alias.symlink_to(bundle / "ресурси", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {type(exc).__name__}")
    artifact = tmp_path / "release.zip"
    artifact.write_bytes(b"existing approved artifact")
    with pytest.raises(ValueError, match="directory symlink"):
        _publish(bundle, artifact)
    assert artifact.read_bytes() == b"existing approved artifact"
    assert not list(tmp_path.glob(".nika-release-*"))


@pytest.mark.skipif(os.name != "nt", reason="Windows junction requires Windows")
def test_windows_junction_cannot_hide_release_tree(tmp_path: Path) -> None:
    import subprocess

    bundle = _bundle(tmp_path)
    target = bundle / "ресурси"
    junction = bundle / "junction"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("Windows junction creation is unavailable")
    artifact = tmp_path / "release.zip"
    artifact.write_bytes(b"existing approved artifact")
    with pytest.raises(ValueError, match="junction"):
        _publish(bundle, artifact)
    assert artifact.read_bytes() == b"existing approved artifact"
    assert not list(tmp_path.glob(".nika-release-*"))
