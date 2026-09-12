from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.research.blobs import ContentAddressedBlobStore


class HostileWorkspace(str):
    def encode(self, *args: object, **kwargs: object) -> bytes:
        return b"attacker-controlled-workspace"

    def __eq__(self, other: object) -> bool:
        return True


def test_put_and_resolve_digest_share_exact_workspace_identity(tmp_path: Path) -> None:
    store = ContentAddressedBlobStore(tmp_path / "blobs")

    artifact = store.put_bytes("workspace-alpha", b"payload")
    resolved = store.resolve_digest(
        "workspace-alpha",
        artifact.raw_sha256,
        artifact.byte_size,
    )

    assert resolved.read_bytes() == b"payload"
    assert artifact.storage_relpath.startswith(
        "a0ba8c07d0462e6b04bfa92a886c9524eb023b8b3d788f933c6d033c076d569f/"
    )


def test_put_rejects_workspace_subclass_before_hash_or_storage(tmp_path: Path) -> None:
    store = ContentAddressedBlobStore(tmp_path / "blobs")

    with pytest.raises(TypeError, match="exact text"):
        store.put_bytes(HostileWorkspace("workspace-alpha"), b"payload")

    assert list((tmp_path / "blobs").glob("**/*")) == []


def test_resolve_rejects_workspace_subclass_before_path_resolution(tmp_path: Path) -> None:
    store = ContentAddressedBlobStore(tmp_path / "blobs")
    artifact = store.put_bytes("workspace-alpha", b"payload")

    with pytest.raises(TypeError, match="exact text"):
        store.resolve_digest(
            HostileWorkspace("workspace-alpha"),
            artifact.raw_sha256,
            artifact.byte_size,
        )


def test_put_rejects_bool_and_unbounded_max_bytes(tmp_path: Path) -> None:
    store = ContentAddressedBlobStore(tmp_path / "blobs")

    with pytest.raises(TypeError, match="exact integer"):
        store.put_bytes("workspace-alpha", b"payload", max_bytes=True)
    with pytest.raises(ValueError, match="signed 64-bit"):
        store.put_bytes("workspace-alpha", b"payload", max_bytes=1 << 63)


def test_put_rejects_noncanonical_workspace_text(tmp_path: Path) -> None:
    store = ContentAddressedBlobStore(tmp_path / "blobs")

    for workspace_id in (" workspace-alpha", "workspace-alpha ", "workspace\nalpha"):
        with pytest.raises(ValueError):
            store.put_bytes(workspace_id, b"payload")
