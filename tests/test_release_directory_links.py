from __future__ import annotations

import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from nika_core.packaging.release import (
    build_release_archive,
    build_release_manifest,
    verify_release_archive,
    verify_release_manifest,
    write_release_manifest,
)

SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"
VERSION = "1.0.0"


def _bundle(tmp_path: Path) -> Path:
    root = tmp_path / "Ніка — пакет"
    root.mkdir()
    (root / "NikaCore.exe").write_bytes(b"standalone executable")
    resources = root / "resources"
    resources.mkdir()
    (resources / "model.bin").write_bytes(b"required model resource")
    return root


def _manifest(root: Path) -> None:
    write_release_manifest(
        root,
        build_release_manifest(
            root, product="NikaCore", version=VERSION, source_sha=SOURCE_SHA
        ),
    )


def _internal_directory_link(root: Path) -> Path:
    link = root / "linked-resources"
    try:
        link.symlink_to(root / "resources", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable on this runner: {exc}")
    assert (link / "model.bin").read_bytes() == b"required model resource"
    return link


def test_manifest_refuses_contained_directory_link_instead_of_omitting_files(
    tmp_path: Path,
) -> None:
    root = _bundle(tmp_path)
    _internal_directory_link(root)

    with pytest.raises(ValueError, match="bundle directory symlink is unsupported"):
        build_release_manifest(
            root, product="NikaCore", version=VERSION, source_sha=SOURCE_SHA
        )


def test_existing_archive_survives_directory_link_added_after_manifest(
    tmp_path: Path,
) -> None:
    root = _bundle(tmp_path)
    _manifest(root)
    manifest = build_release_manifest(
        root, product="NikaCore", version=VERSION, source_sha=SOURCE_SHA
    )
    _internal_directory_link(root)
    artifact = tmp_path / "approved.zip"
    artifact.write_bytes(b"previous approved artifact")

    with pytest.raises(ValueError, match="bundle directory symlink is unsupported"):
        verify_release_manifest(root, manifest)
    with pytest.raises(ValueError, match="bundle directory symlink is unsupported"):
        build_release_archive(
            root,
            artifact,
            source_sha=SOURCE_SHA,
            expected_product_version=VERSION,
        )
    assert artifact.read_bytes() == b"previous approved artifact"
    assert not list(tmp_path.glob(".nika-release-*"))


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_windows_junction_cannot_hide_unenumerated_resource_tree(
    tmp_path: Path,
) -> None:
    root = _bundle(tmp_path)
    link = root / "junction-resources"
    try:
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(root / "resources")],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        pytest.skip(f"junction creation unavailable: {exc}")
    if result.returncode:
        pytest.skip("Windows runner does not permit junction creation")
    assert link.is_junction()
    assert (link / "model.bin").is_file()

    with pytest.raises(ValueError, match="bundle junction is unsupported"):
        build_release_manifest(
            root, product="NikaCore", version=VERSION, source_sha=SOURCE_SHA
        )


def test_contained_file_symlink_is_materialized_as_a_regular_zip_member(
    tmp_path: Path,
) -> None:
    root = _bundle(tmp_path)
    link = root / "alias.bin"
    try:
        link.symlink_to(root / "resources" / "model.bin")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"file symlinks unavailable on this runner: {exc}")
    _manifest(root)
    artifact = tmp_path / "complete.zip"

    build_release_archive(
        root,
        artifact,
        source_sha=SOURCE_SHA,
        expected_product_version=VERSION,
    )
    assert verify_release_archive(
        artifact, source_sha=SOURCE_SHA, expected_product_version=VERSION
    ) == ()
    with zipfile.ZipFile(artifact) as archive:
        assert archive.read("alias.bin") == b"required model resource"
        assert archive.read("resources/model.bin") == b"required model resource"
