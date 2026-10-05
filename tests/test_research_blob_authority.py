from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from nika_core.research.blobs import BlobStoreError, ContentAddressedBlobStore


def test_blob_roundtrip_dedup_and_restart_for_unicode_workspace(tmp_path: Path) -> None:
    store = ContentAddressedBlobStore(tmp_path / "blobs")
    first = store.put_bytes("дослідження", b"stable raw bytes")
    second = store.put_bytes("дослідження", b"stable raw bytes")
    assert first == second
    assert store.resolve(first).read_bytes() == b"stable raw bytes"
    assert ContentAddressedBlobStore(store.root).resolve(second).read_bytes() == b"stable raw bytes"


def test_identical_bytes_have_distinct_workspace_artifact_identity(tmp_path: Path) -> None:
    store = ContentAddressedBlobStore(tmp_path / "blobs")
    first = store.put_bytes("workspace-a", b"common payload")
    second = store.put_bytes("workspace-b", b"common payload")
    assert first.raw_sha256 == second.raw_sha256
    assert first.artifact_id != second.artifact_id
    assert first.storage_relpath != second.storage_relpath
    assert store.resolve(first).read_bytes() == store.resolve(second).read_bytes()


def test_resolve_rejects_workspace_and_artifact_metadata_substitution(tmp_path: Path) -> None:
    store = ContentAddressedBlobStore(tmp_path / "blobs")
    first = store.put_bytes("workspace-a", b"common payload")
    second = store.put_bytes("workspace-b", b"common payload")
    for forged, diagnostic in (
        (replace(first, workspace_id="workspace-b"), "storage path"),
        (replace(first, storage_relpath=second.storage_relpath), "storage path"),
        (
            replace(first, workspace_id="workspace-b", storage_relpath=second.storage_relpath),
            "artifact ID",
        ),
        (replace(first, artifact_id="0" * 64), "artifact ID"),
        (replace(first, storage_relpath="../other"), "storage path"),
        (replace(first, storage_relpath=first.storage_relpath + "/"), "storage path"),
    ):
        with pytest.raises(BlobStoreError, match=diagnostic):
            store.resolve(forged)
    assert store.resolve(first).read_bytes() == b"common payload"


@pytest.mark.parametrize("raw", ["A" * 64, "0" * 63, "g" * 64, None, b"0" * 64])
def test_resolve_rejects_noncanonical_raw_digest(tmp_path: Path, raw: object) -> None:
    store = ContentAddressedBlobStore(tmp_path / "blobs")
    artifact = store.put_bytes("ws", b"raw")
    with pytest.raises(BlobStoreError, match="raw digest"):
        store.resolve(replace(artifact, raw_sha256=raw))


@pytest.mark.parametrize("size", [True, 3.0, -1, "3", None])
def test_resolve_rejects_nonintegral_or_negative_byte_size(tmp_path: Path, size: object) -> None:
    store = ContentAddressedBlobStore(tmp_path / "blobs")
    artifact = store.put_bytes("ws", b"raw")
    with pytest.raises(BlobStoreError, match="byte size"):
        store.resolve(replace(artifact, byte_size=size))


@pytest.mark.parametrize("workspace", ["", "  ", None, b"ws", 12])
def test_resolve_rejects_invalid_workspace_identity(tmp_path: Path, workspace: object) -> None:
    store = ContentAddressedBlobStore(tmp_path / "blobs")
    artifact = store.put_bytes("ws", b"raw")
    with pytest.raises(BlobStoreError, match="workspace identity"):
        store.resolve(replace(artifact, workspace_id=workspace))


@pytest.mark.parametrize("limit", [True, 3.5, float("inf"), 0, -1, None, "4"])
def test_put_rejects_ambiguous_byte_budget_before_writing(tmp_path: Path, limit: object) -> None:
    store = ContentAddressedBlobStore(tmp_path / "blobs")
    with pytest.raises(ValueError, match="positive integer"):
        store.put_bytes("ws", b"raw", max_bytes=limit)
    assert not (store.root / ".tmp").exists()


def test_put_rejects_nonbytes_chunk_and_cleans_temporary_file(tmp_path: Path) -> None:
    store = ContentAddressedBlobStore(tmp_path / "blobs")
    with pytest.raises(BlobStoreError, match="must be bytes"):
        store.put_bytes("ws", bytearray(b"not immutable bytes"))
    assert not list((store.root / ".tmp").iterdir())


def test_put_existing_blob_rejects_digest_mismatch_and_cleans_temp(tmp_path: Path) -> None:
    store = ContentAddressedBlobStore(tmp_path / "blobs")
    artifact = store.put_bytes("ws", b"original")
    store.resolve(artifact).write_bytes(b"tampered")
    with pytest.raises(BlobStoreError, match="digest verification"):
        store.put_bytes("ws", b"original")
    assert not list((store.root / ".tmp").iterdir())


def test_empty_blob_is_valid_and_byte_limit_is_enforced(tmp_path: Path) -> None:
    store = ContentAddressedBlobStore(tmp_path / "blobs")
    empty = store.put_bytes("ws", b"", max_bytes=1)
    assert empty.byte_size == 0
    assert store.resolve(empty).read_bytes() == b""
    with pytest.raises(BlobStoreError, match="storage limit"):
        store.put_bytes("ws", b"two", max_bytes=2)


def _symlink_or_skip(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"filesystem does not permit symlink regression: {exc}")


def test_put_rejects_external_temporary_directory_alias(tmp_path: Path) -> None:
    store = ContentAddressedBlobStore(tmp_path / "blobs")
    external = tmp_path / "external"
    external.mkdir()
    _symlink_or_skip(store.root / ".tmp", external, directory=True)
    with pytest.raises(BlobStoreError, match="trusted directory"):
        store.put_bytes("ws", b"never outside")
    assert not list(external.iterdir())


def test_put_rejects_external_workspace_directory_alias(tmp_path: Path) -> None:
    store = ContentAddressedBlobStore(tmp_path / "blobs")
    artifact = store.put_bytes("ws", b"known")
    workspace_dir = (store.root / artifact.storage_relpath).parent.parent
    external = tmp_path / "external"
    external.mkdir()
    # Remove the controlled blob tree before installing an existing foreign alias.
    stored = store.resolve(artifact)
    stored.unlink()
    stored.parent.rmdir()
    workspace_dir.rmdir()
    _symlink_or_skip(workspace_dir, external, directory=True)
    with pytest.raises(BlobStoreError, match="trusted directory"):
        store.put_bytes("ws", b"known")
    assert not list(external.iterdir())


def test_resolve_rejects_link_to_external_blob(tmp_path: Path) -> None:
    store = ContentAddressedBlobStore(tmp_path / "blobs")
    artifact = store.put_bytes("ws", b"known")
    stored = store.resolve(artifact)
    external = tmp_path / "external.bin"
    external.write_bytes(stored.read_bytes())
    stored.unlink()
    _symlink_or_skip(stored, external)
    with pytest.raises(BlobStoreError, match="symbolic links"):
        store.resolve(artifact)
