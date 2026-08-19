from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from nika_core.research.models import BlobArtifact


class BlobStoreError(RuntimeError):
    pass


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

    def put_file(
        self,
        workspace_id: str,
        source_path: Path | str,
        *,
        max_bytes: int = 64 * 1024 * 1024,
    ) -> BlobArtifact:
        if not workspace_id.strip():
            raise ValueError("workspace_id is required")
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")

        source = Path(source_path)
        if not source.is_file():
            raise BlobStoreError("artifact source is not a regular file")

        workspace_key = hashlib.sha256(workspace_id.encode()).hexdigest()
        temp_dir = self.root / ".tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        total = 0
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=temp_dir, delete=False) as temp:
                temp_path = Path(temp.name)
                with source.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        total += len(chunk)
                        if total > max_bytes:
                            raise BlobStoreError(
                                f"artifact exceeds {max_bytes} byte storage limit"
                            )
                        digest.update(chunk)
                        temp.write(chunk)
                temp.flush()
                os.fsync(temp.fileno())

            raw_sha256 = digest.hexdigest()
            relative = Path(workspace_key) / raw_sha256[:2] / raw_sha256
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if destination.stat().st_size != total:
                    raise BlobStoreError("existing content-addressed blob has unexpected size")
                if _sha256_file(destination) != raw_sha256:
                    raise BlobStoreError("existing content-addressed blob failed digest verification")
                temp_path.unlink(missing_ok=True)
            else:
                os.replace(temp_path, destination)
            artifact_id = hashlib.sha256(
                f"{workspace_id}\0{raw_sha256}".encode()
            ).hexdigest()
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
