from __future__ import annotations

import hashlib
import os
import re
import tempfile
from collections.abc import Iterable
from pathlib import Path

from nika_core.research.models import BlobArtifact


class BlobStoreError(RuntimeError):
    pass


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _workspace_key(workspace_id: str) -> str:
    if type(workspace_id) is not str or not workspace_id.strip():
        raise ValueError("workspace_id must be nonempty text")
    return hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class ContentAddressedBlobStore:
    """Workspace-namespaced, content-addressed raw artifact storage."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _ensure_directory(self, path: Path) -> None:
        # Existing aliases must not redirect a workspace or temporary write outside the root.
        if path.is_symlink() or not path.parent.resolve().is_relative_to(self.root):
            raise BlobStoreError("blob storage directory is not a trusted directory")
        path.mkdir(exist_ok=True)
        if path.is_symlink() or not path.is_dir() or not path.resolve().is_relative_to(self.root):
            raise BlobStoreError("blob storage directory is not a trusted directory")

    def _put_chunks(
        self,
        workspace_id: str,
        chunks: Iterable[bytes],
        *,
        max_bytes: int,
    ) -> BlobArtifact:
        workspace_key = _workspace_key(workspace_id)
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")

        temp_dir = self.root / ".tmp"
        self._ensure_directory(temp_dir)
        digest = hashlib.sha256()
        total = 0
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=temp_dir, delete=False) as temp:
                temp_path = Path(temp.name)
                for chunk in chunks:
                    if type(chunk) is not bytes:
                        raise BlobStoreError("artifact chunks must be bytes")
                    total += len(chunk)
                    if total > max_bytes:
                        raise BlobStoreError(f"artifact exceeds {max_bytes} byte storage limit")
                    digest.update(chunk)
                    temp.write(chunk)
                temp.flush()
                os.fsync(temp.fileno())

            raw_sha256 = digest.hexdigest()
            relative = Path(workspace_key) / raw_sha256[:2] / raw_sha256
            destination = self.root / relative
            self._ensure_directory(self.root / workspace_key)
            self._ensure_directory(destination.parent)
            if destination.is_symlink():
                raise BlobStoreError("content-addressed blob must not be a symbolic link")
            if destination.exists():
                if destination.stat().st_size != total:
                    raise BlobStoreError("existing content-addressed blob has unexpected size")
                if _sha256_file(destination) != raw_sha256:
                    raise BlobStoreError(
                        "existing content-addressed blob failed digest verification"
                    )
                temp_path.unlink(missing_ok=True)
            else:
                os.replace(temp_path, destination)
            artifact_id = hashlib.sha256(f"{workspace_id}\0{raw_sha256}".encode()).hexdigest()
            return BlobArtifact(
                artifact_id=artifact_id,
                workspace_id=workspace_id,
                raw_sha256=raw_sha256,
                byte_size=total,
                storage_relpath=relative.as_posix(),
            )
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
        if type(artifact) is not BlobArtifact:
            raise BlobStoreError("artifact metadata has an invalid carrier")
        try:
            workspace_key = _workspace_key(artifact.workspace_id)
        except (TypeError, ValueError, UnicodeError) as exc:
            raise BlobStoreError("artifact workspace identity is invalid") from exc
        if type(artifact.raw_sha256) is not str or _SHA256.fullmatch(artifact.raw_sha256) is None:
            raise BlobStoreError("artifact raw digest is invalid")
        if type(artifact.byte_size) is not int or artifact.byte_size < 0:
            raise BlobStoreError("artifact byte size is invalid")

        relative = Path(workspace_key) / artifact.raw_sha256[:2] / artifact.raw_sha256
        if (
            type(artifact.storage_relpath) is not str
            or artifact.storage_relpath != relative.as_posix()
        ):
            raise BlobStoreError("artifact storage path does not match workspace and digest")
        expected_id = hashlib.sha256(
            f"{artifact.workspace_id}\0{artifact.raw_sha256}".encode("utf-8")
        ).hexdigest()
        if type(artifact.artifact_id) is not str or artifact.artifact_id != expected_id:
            raise BlobStoreError("artifact ID does not match workspace and digest")

        candidate = self.root / relative
        path_parts = (candidate.parent.parent, candidate.parent, candidate)
        if any(path.is_symlink() for path in path_parts):
            raise BlobStoreError("artifact path must not contain symbolic links")
        if not candidate.resolve().is_relative_to(self.root):
            raise BlobStoreError("artifact storage path escapes blob root")
        if not candidate.is_file():
            raise BlobStoreError("content-addressed blob is missing")
        if candidate.stat().st_size != artifact.byte_size:
            raise BlobStoreError("content-addressed blob size does not match metadata")
        if _sha256_file(candidate) != artifact.raw_sha256:
            raise BlobStoreError("content-addressed blob digest does not match metadata")
        return candidate
