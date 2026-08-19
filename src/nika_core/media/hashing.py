from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from nika_core.media.errors import MediaError, MediaErrorCode


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_bytes(payload.encode("utf-8"))


def sha256_file(path: Path, *, max_bytes: int | None = None, chunk_size: int = 1024 * 1024) -> str:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if not path.is_file():
        raise MediaError(MediaErrorCode.SOURCE_NOT_FOUND, f"file not found: {path.name}")
    size = path.stat().st_size
    if max_bytes is not None and size > max_bytes:
        raise MediaError(
            MediaErrorCode.SOURCE_TOO_LARGE,
            f"file exceeds configured size limit ({size} > {max_bytes} bytes)",
        )
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def resolve_bounded_path(path: Path, *, allowed_root: Path) -> Path:
    resolved_root = allowed_root.resolve(strict=True)
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise MediaError(MediaErrorCode.PATH_ESCAPE, "media path escapes the allowed root") from exc
    return resolved
