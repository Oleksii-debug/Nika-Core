from __future__ import annotations

import mimetypes
import uuid
from dataclasses import dataclass
from pathlib import Path

from nika_core.media.contracts import (
    AssetKind,
    MediaAsset,
    MediaSource,
    MediaSourceKind,
    MediaVersion,
)
from nika_core.media.hashing import resolve_bounded_path, sha256_file, sha256_json


@dataclass(frozen=True, slots=True)
class LocalImportResult:
    source: MediaSource
    version: MediaVersion
    asset: MediaAsset
    absolute_path: Path


def import_local_media(
    path: Path,
    *,
    allowed_root: Path,
    max_bytes: int = 8 * 1024 * 1024 * 1024,
    privacy: str = "private",
) -> LocalImportResult:
    resolved = resolve_bounded_path(path, allowed_root=allowed_root)
    size = resolved.stat().st_size
    checksum = sha256_file(resolved, max_bytes=max_bytes)
    source_id = f"local:{sha256_json({'path': str(resolved)})[:32]}"
    version_id = f"media:{checksum[:32]}"
    media_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
    source = MediaSource(
        source_id=source_id,
        kind=MediaSourceKind.LOCAL_FILE,
        locator=str(resolved),
        privacy=privacy,
    )
    metadata = {
        "name": resolved.name,
        "size_bytes": size,
        "media_type": media_type,
        "content_sha256": checksum,
    }
    version = MediaVersion(
        version_id=version_id,
        source_id=source_id,
        metadata_sha256=sha256_json(metadata),
        content_sha256=checksum,
        title=resolved.stem,
    )
    asset = MediaAsset(
        asset_id=str(uuid.uuid4()),
        version_id=version_id,
        kind=AssetKind.ORIGINAL,
        relative_path=resolved.name,
        sha256=checksum,
        size_bytes=size,
        media_type=media_type,
        immutable_original=True,
    )
    return LocalImportResult(source=source, version=version, asset=asset, absolute_path=resolved)
