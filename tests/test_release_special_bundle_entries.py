from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path

import pytest

from nika_core.packaging.release import (
    build_release_manifest,
    verify_release_manifest,
    write_release_manifest,
)

SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"
VERSION = "1.0.0"


def _bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "NikaCore.exe").write_bytes(b"windows executable")
    resources = bundle / "resources"
    resources.mkdir()
    (resources / "required.bin").write_bytes(b"required resource")
    return bundle


def _manifest(bundle: Path):
    return build_release_manifest(
        bundle,
        product="NikaCore",
        version=VERSION,
        source_sha=SOURCE_SHA,
    )


def _directory_symlink(path: Path, target: Path) -> None:
    try:
        path.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlink creation unavailable: {exc}")


@pytest.mark.skipif(
    os.name == "nt" or not hasattr(os, "mkfifo"),
    reason="POSIX named-pipe regression",
)
def test_named_pipe_is_rejected_without_opening_or_silently_omitting(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    os.mkfifo(bundle / "unreadable.fifo")

    with pytest.raises(ValueError, match="unsupported release bundle entry"):
        _manifest(bundle)


@pytest.mark.skipif(
    os.name == "nt" or not hasattr(os, "mkfifo"),
    reason="POSIX named-pipe regression",
)
def test_post_manifest_pipe_rejects_verification(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    manifest = _manifest(bundle)
    write_release_manifest(bundle, manifest)
    os.mkfifo(bundle / "unlisted.fifo")

    with pytest.raises(ValueError, match="unsupported release bundle entry"):
        verify_release_manifest(bundle, manifest)


@pytest.mark.skipif(
    os.name == "nt" or not hasattr(socket, "AF_UNIX"),
    reason="POSIX Unix-domain socket regression",
)
def test_unix_socket_is_rejected_instead_of_disappearing_from_release(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    address = bundle / "worker.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        try:
            server.bind(str(address))
        except OSError as exc:
            pytest.skip(f"Unix-domain socket fixture unavailable: {exc}")
        with pytest.raises(ValueError, match="unsupported release bundle entry"):
            _manifest(bundle)


def test_directory_symlink_inside_bundle_is_rejected_before_traversal(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    (external / "private.bin").write_bytes(b"private")
    _directory_symlink(bundle / "linked-directory", external)

    with pytest.raises(
        ValueError,
        match="(?:directory symlink is unsupported|symlink escapes release root)",
    ):
        _manifest(bundle)


def test_regular_directories_remain_supported(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    (bundle / "empty-directory").mkdir()
    manifest = _manifest(bundle)

    assert {entry.path for entry in manifest.files} == {
        "NikaCore.exe",
        "resources/required.bin",
    }
    write_release_manifest(bundle, manifest)
    assert verify_release_manifest(bundle, manifest) == ()


def test_nested_directory_enumeration_failure_cannot_verify_incomplete_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle(tmp_path)
    manifest = _manifest(bundle)
    write_release_manifest(bundle, manifest)
    original_iterdir = Path.iterdir

    def refuse_nested_directory(path: Path):
        if path == bundle / "resources":
            raise PermissionError("simulated inaccessible bundle directory")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", refuse_nested_directory)
    with pytest.raises(PermissionError, match="inaccessible bundle directory"):
        verify_release_manifest(bundle, manifest)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction-only regression")
def test_windows_bundle_junction_is_rejected_before_traversal(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    external = tmp_path / "external-junction"
    external.mkdir()
    (external / "private.bin").write_bytes(b"private")
    junction = bundle / "junction"
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(external)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        pytest.skip("Windows junction creation unavailable on this runner")

    assert junction.is_junction()
    with pytest.raises(ValueError, match="bundle junction is unsupported"):
        _manifest(bundle)
