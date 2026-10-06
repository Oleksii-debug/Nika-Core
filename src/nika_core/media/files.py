from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.hashing import sha256_file


@dataclass(frozen=True, slots=True)
class PromotedFile:
    path: Path
    sha256: str
    size_bytes: int


def promote_partial_file(
    partial_path: Path,
    final_path: Path,
    *,
    allowed_root: Path,
    expected_sha256: str | None = None,
    max_bytes: int | None = None,
) -> PromotedFile:
    root = allowed_root.resolve(strict=True)
    partial = partial_path.resolve(strict=True)
    final_parent = final_path.parent.resolve(strict=True)
    try:
        partial.relative_to(root)
        final_parent.relative_to(root)
    except ValueError as exc:
        raise MediaError(MediaErrorCode.PATH_ESCAPE, "media output escapes the allowed root") from exc
    if partial.suffix != ".partial":
        raise ValueError("partial media output must use the .partial suffix")
    if final_path.exists():
        raise FileExistsError(f"refusing to overwrite existing media output: {final_path.name}")
    checksum = sha256_file(partial, max_bytes=max_bytes)
    if expected_sha256 is not None and checksum != expected_sha256.lower():
        raise MediaError(MediaErrorCode.CHECKSUM_MISMATCH, "media output checksum did not match")
    size = partial.stat().st_size
    os.replace(partial, final_path)
    return PromotedFile(path=final_path, sha256=checksum, size_bytes=size)
