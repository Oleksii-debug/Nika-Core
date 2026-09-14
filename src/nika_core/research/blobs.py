from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path

from nika_core.research.models import BlobArtifact

_HEX_DIGITS = frozenset("0123456789abcdef")
_MAX_SIGNED_64 = (1 << 63) - 1
_MAX_WORKSPACE_BYTES = 4096


class BlobStoreError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _workspace_key(workspace_id: str) -> str:
    if type(workspace_id) is not str:
        raise TypeError("workspace_id must be exact text")
    if not workspace_id or workspace_id != workspace_id.strip():
        raise ValueError("workspace_id must be non-empty without surrounding whitespace")
    if any(ord(character) < 32 or ord(character) == 127 for character in workspace_id):
        raise ValueError("workspace_id must not contain control characters")
    encoded = workspace_id.encode("utf-8")
    if len(encoded) > _MAX_WORKSPACE_BYTES:
        raise ValueError("workspace_id exceeds the configured byte limit")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_blob_artifact(
    workspace_id: str,
    raw_sha256: str,
    byte_size: int,
) -> BlobArtifact:
    workspace_key = _workspace_key(workspace_id)
    if (
        type(raw_sha256) is not str
        or len(raw_sha256) != 64
        or any(character not in _HEX_DIGITS for character in raw_sha256)
    ):
        raise ValueError("raw_sha256 must be an exact lowercase SHA-256 digest")
    if (
        type(byte_size) is not int
        or byte_size < 0
        or byte_size > _MAX_SIGNED_64
    ):
        raise ValueError("byte_size must be an integer from 0 through signed 64-bit max")
    relative = Path(workspace_key) / raw_sha256[:2] / raw_sha256
    artifact_id = hashlib.sha256(f"{workspace_id}\0{raw_sha256}".encode()).hexdigest()
    return BlobArtifact(
        artifact_id=artifact_id,
        workspace_id=workspace_id,
        raw_sha256=raw_sha256,
        byte_size=byte_size,
        storage_relpath=relative.as_posix(),
    )


class ContentAddressedBlobStore:
    """Workspace-namespaced, content-addressed raw artifact storage."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _put_chunks(
        self,
        workspace_id: str,
        chunks: Iterable[bytes],
        *,
        max_bytes: int,
    ) -> BlobArtifact:
        _workspace_key(workspace_id)
        if type(max_bytes) is not int:
            raise TypeError("max_bytes must be an exact integer")
        if max_bytes < 1 or max_bytes > _MAX_SIGNED_64:
            raise ValueError("max_bytes must be in the signed 64-bit positive range")

        temp_dir = self.root / ".tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        total = 0
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=temp_dir, delete=False) as temp:
                temp_path = Path(temp.name)
                for chunk in chunks:
                    total += len(chunk)
                    if total > max_bytes:
                        raise BlobStoreError(f"artifact exceeds {max_bytes} byte storage limit")
                    digest.update(chunk)
                    temp.write(chunk)
                temp.flush()
                os.fsync(temp.fileno())

            artifact = _canonical_blob_artifact(workspace_id, digest.hexdigest(), total)
            destination = self.root / artifact.storage_relpath
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if destination.stat().st_size != total:
                    raise BlobStoreError("existing content-addressed blob has unexpected size")
                if _sha256_file(destination) != artifact.raw_sha256:
                    raise BlobStoreError("existing content-addressed blob failed digest verification")
                temp_path.unlink(missing_ok=True)
            else:
                os.replace(temp_path, destination)
            return artifact
        except Exception:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            raise

    def put_file(
        self,
        workspace_id: str,
        source_path: Path | str,
        *,
        max_bytes: int = 64 * 1024 * 1024,
    ) -> BlobArtifact:
        source = Path(source_path)
        if not source.is_file():
            raise BlobStoreError("artifact source is not a regular file")

        def chunks() -> Iterable[bytes]:
            with source.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    yield chunk

        return self._put_chunks(workspace_id, chunks(), max_bytes=max_bytes)

    def put_bytes(
        self,
        workspace_id: str,
        payload: bytes,
        *,
        max_bytes: int = 64 * 1024 * 1024,
    ) -> BlobArtifact:
        return self._put_chunks(workspace_id, (payload,), max_bytes=max_bytes)

    def resolve(self, artifact: BlobArtifact) -> Path:
        candidate = (self.root / artifact.storage_relpath).resolve()
        if not candidate.is_relative_to(self.root):
            raise BlobStoreError("artifact storage path escapes blob root")
        if not candidate.is_file():
            raise BlobStoreError("content-addressed blob is missing")
        if candidate.stat().st_size != artifact.byte_size:
            raise BlobStoreError("content-addressed blob size does not match metadata")
        if _sha256_file(candidate) != artifact.raw_sha256:
            raise BlobStoreError("content-addressed blob digest does not match metadata")
        return candidate

    def resolve_digest(
        self,
        workspace_id: str,
        raw_sha256: str,
        byte_size: int,
    ) -> Path:
        """Resolve and reverify exact content identity without inventing a second store."""
        artifact = _canonical_blob_artifact(workspace_id, raw_sha256, byte_size)
        return self.resolve(artifact)
