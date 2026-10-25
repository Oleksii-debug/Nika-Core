from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

_TREE_DIGEST_MAGIC = b"nika-foundry-model-cache-tree-v2\x00"
_LENGTH_BYTES = 8
_READ_CHUNK_BYTES = 1024 * 1024


def _length_prefix(value: int) -> bytes:
    if value < 0:
        raise ValueError("length prefix cannot encode a negative value")
    return value.to_bytes(_LENGTH_BYTES, byteorder="big", signed=False)


def _lstat_plain(path: Path) -> os.stat_result:
    try:
        info = os.lstat(path)
    except OSError:
        raise ValueError("model cache path cannot be inspected") from None

    attributes = int(getattr(info, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    if stat.S_ISLNK(info.st_mode) or (reparse_flag and attributes & reparse_flag):
        raise ValueError("model cache contains filesystem indirection")
    return info


def _relative_path(root: Path, path: Path) -> Path:
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise ValueError("model cache path escapes selected root") from None
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("model cache path escapes selected root")
    return relative


def _resolved_within_root(root: Path, path: Path) -> Path:
    try:
        resolved_root = root.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
        resolved_path.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        raise ValueError("model cache path escapes selected root") from None
    return resolved_path


def _cache_files(root: Path) -> list[tuple[Path, os.stat_result]]:
    root = root.absolute()
    root_info = _lstat_plain(root)
    if not stat.S_ISDIR(root_info.st_mode):
        raise ValueError("model cache path is not a directory")
    _resolved_within_root(root, root)

    def fail_walk(_error: OSError) -> None:
        raise ValueError("model cache tree cannot be enumerated") from None

    files: list[tuple[Path, os.stat_result]] = []
    try:
        for directory, directory_names, file_names in os.walk(
            root,
            topdown=True,
            onerror=fail_walk,
            followlinks=False,
        ):
            base = Path(directory)
            _relative_path(root, base)
            _resolved_within_root(root, base)
            base_info = _lstat_plain(base)
            if not stat.S_ISDIR(base_info.st_mode):
                raise ValueError("model cache entry is not a directory")

            for name in directory_names:
                child = base / name
                _relative_path(root, child)
                child_info = _lstat_plain(child)
                if not stat.S_ISDIR(child_info.st_mode):
                    raise ValueError("model cache entry is not a directory")
                _resolved_within_root(root, child)

            for name in file_names:
                child = base / name
                _relative_path(root, child)
                child_info = _lstat_plain(child)
                if not stat.S_ISREG(child_info.st_mode):
                    raise ValueError("model cache entry is not a regular file")
                _resolved_within_root(root, child)
                files.append((child, child_info))
    except OSError:
        raise ValueError("model cache tree cannot be enumerated") from None

    files.sort(key=lambda item: _relative_path(root, item[0]).as_posix())
    if not files:
        raise ValueError("model cache path contains no regular files")
    return files


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
    )


def _file_anchor_identity(info: os.stat_result) -> tuple[int, int, int]:
    """Cross-stat identity fields used to bind an opened descriptor to its pathname."""

    return (int(info.st_dev), int(info.st_ino), int(info.st_size))


def _inventory_identity(
    root: Path,
    files: list[tuple[Path, os.stat_result]],
) -> tuple[tuple[str, tuple[int, int, int, int, int]], ...]:
    return tuple(
        (_relative_path(root, path).as_posix(), _file_identity(info))
        for path, info in files
    )


def _hash_file(
    digest: hashlib._Hash,
    *,
    root: Path,
    path: Path,
    before: os.stat_result,
) -> int:
    _resolved_within_root(root, path)
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0)) | int(getattr(os, "O_NOFOLLOW", 0))
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise ValueError("model cache file cannot be opened") from None

    observed_size = 0
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("model cache file changed before hashing")

        # Keep pathname metadata comparisons in their own stat domain because Python's
        # Windows path-stat and descriptor-stat APIs can expose different ctime values.
        # Device + inode + size are nevertheless the stable cross-domain anchor that
        # proves the descriptor we are about to read is the file inventoried at this
        # pathname. Without that binding, a same-size replacement could be opened and
        # the original pathname restored before the second lstat(), yielding evidence
        # for bytes that are no longer present in the final inventory.
        after_open = _lstat_plain(path)
        _resolved_within_root(root, path)
        if (
            _file_identity(after_open) != _file_identity(before)
            or _file_anchor_identity(opened) != _file_anchor_identity(before)
        ):
            raise ValueError("model cache file changed before hashing")

        opened_identity = _file_identity(opened)
        while True:
            chunk = os.read(descriptor, _READ_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            observed_size += len(chunk)
        opened_after = os.fstat(descriptor)
        if _file_identity(opened_after) != opened_identity:
            raise ValueError("model cache file changed while hashing")
    except OSError:
        raise ValueError("model cache file cannot be read") from None
    finally:
        os.close(descriptor)

    after = _lstat_plain(path)
    _resolved_within_root(root, path)
    if observed_size != int(before.st_size) or _file_identity(after) != _file_identity(before):
        raise ValueError("model cache file changed while hashing")
    return observed_size


def foundry_cache_tree_sha256(root: Path) -> dict[str, object]:
    """Hash one concrete Foundry model cache tree with unambiguous framing.

    The digest binds exact relative path bytes, file sizes and file bytes. Symbolic links,
    Windows reparse points, path escapes, incomplete enumeration and detected concurrent
    mutations fail closed.
    """

    root = root.absolute()
    files = _cache_files(root)
    initial_inventory = _inventory_identity(root, files)
    digest = hashlib.sha256()
    digest.update(_TREE_DIGEST_MAGIC)
    digest.update(_length_prefix(len(files)))
    total_bytes = 0

    for path, before in files:
        relative_bytes = _relative_path(root, path).as_posix().encode("utf-8")
        expected_size = int(before.st_size)
        digest.update(_length_prefix(len(relative_bytes)))
        digest.update(relative_bytes)
        digest.update(_length_prefix(expected_size))
        total_bytes += _hash_file(digest, root=root, path=path, before=before)

    final_files = _cache_files(root)
    if _inventory_identity(root, final_files) != initial_inventory:
        raise ValueError("model cache tree changed while hashing")

    return {
        "algorithm": "sha256-tree-v2",
        "sha256": digest.hexdigest(),
        "file_count": len(files),
        "total_bytes": total_bytes,
    }
