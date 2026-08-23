from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

_TREE_DIGEST_MAGIC = b"nika-foundry-model-cache-tree-v2\x00"
_LENGTH_BYTES = 8


def _length_prefix(value: int) -> bytes:
    if value < 0:
        raise ValueError("length prefix cannot encode a negative value")
    return value.to_bytes(_LENGTH_BYTES, byteorder="big", signed=False)


def _lstat_plain(path: Path) -> os.stat_result:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ValueError(f"model cache path cannot be inspected: {path}") from exc

    attributes = int(getattr(info, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    if path.is_symlink() or (reparse_flag and attributes & reparse_flag):
        raise ValueError(f"model cache contains filesystem indirection: {path}")
    return info


def _cache_files(root: Path) -> list[tuple[Path, os.stat_result]]:
    root_info = _lstat_plain(root)
    if not stat.S_ISDIR(root_info.st_mode):
        raise ValueError(f"model cache path is not a directory: {root}")

    def fail_walk(error: OSError) -> None:
        raise ValueError(f"model cache tree cannot be enumerated: {root}") from error

    files: list[tuple[Path, os.stat_result]] = []
    try:
        for directory, directory_names, file_names in os.walk(
            root,
            topdown=True,
            onerror=fail_walk,
            followlinks=False,
        ):
            base = Path(directory)
            for name in directory_names:
                child = base / name
                child_info = _lstat_plain(child)
                if not stat.S_ISDIR(child_info.st_mode):
                    raise ValueError(f"model cache entry is not a directory: {child}")
            for name in file_names:
                child = base / name
                child_info = _lstat_plain(child)
                if not stat.S_ISREG(child_info.st_mode):
                    raise ValueError(f"model cache entry is not a regular file: {child}")
                files.append((child, child_info))
    except OSError as exc:
        raise ValueError(f"model cache tree cannot be enumerated: {root}") from exc

    files.sort(key=lambda item: item[0].relative_to(root).as_posix())
    if not files:
        raise ValueError(f"model cache path contains no regular files: {root}")
    return files


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
    )


def _inventory_identity(
    root: Path,
    files: list[tuple[Path, os.stat_result]],
) -> tuple[tuple[str, tuple[int, int, int, int, int]], ...]:
    return tuple(
        (path.relative_to(root).as_posix(), _file_identity(info))
        for path, info in files
    )


def foundry_cache_tree_sha256(root: Path) -> dict[str, object]:
    """Hash one concrete Foundry model cache tree with unambiguous framing.

    The digest is evidence over exact relative path bytes, file sizes and file bytes.
    Symbolic links and Windows reparse points fail closed so cache evidence cannot
    silently incorporate bytes outside the selected model cache tree.
    """

    files = _cache_files(root)
    initial_inventory = _inventory_identity(root, files)
    digest = hashlib.sha256()
    digest.update(_TREE_DIGEST_MAGIC)
    digest.update(_length_prefix(len(files)))
    total_bytes = 0

    for path, before in files:
        relative_bytes = path.relative_to(root).as_posix().encode("utf-8")
        expected_size = int(before.st_size)
        digest.update(_length_prefix(len(relative_bytes)))
        digest.update(relative_bytes)
        digest.update(_length_prefix(expected_size))

        observed_size = 0
        try:
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
                    observed_size += len(chunk)
        except OSError as exc:
            raise ValueError(f"model cache file cannot be read: {path}") from exc

        after = _lstat_plain(path)
        if observed_size != expected_size or _file_identity(after) != _file_identity(before):
            raise ValueError(f"model cache file changed while hashing: {path}")
        total_bytes += observed_size

    final_files = _cache_files(root)
    if _inventory_identity(root, final_files) != initial_inventory:
        raise ValueError("model cache tree changed while hashing")

    return {
        "algorithm": "sha256-tree-v2",
        "sha256": digest.hexdigest(),
        "file_count": len(files),
        "total_bytes": total_bytes,
    }
