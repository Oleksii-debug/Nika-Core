from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from nika_core.model_gateway import foundry_cache_evidence
from nika_core.model_gateway.foundry_cache_evidence import foundry_cache_tree_sha256


def _legacy_tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=str):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def test_v2_framing_separates_distinct_trees_that_alias_under_v1(tmp_path: Path) -> None:
    single = tmp_path / "single"
    split = tmp_path / "split"
    single.mkdir()
    split.mkdir()

    (single / "a").write_bytes(b"X\0b\0Y")
    (split / "a").write_bytes(b"X")
    (split / "b").write_bytes(b"Y")

    assert _legacy_tree_digest(single) == _legacy_tree_digest(split)

    single_evidence = foundry_cache_tree_sha256(single)
    split_evidence = foundry_cache_tree_sha256(split)

    assert single_evidence["algorithm"] == "sha256-tree-v2"
    assert split_evidence["algorithm"] == "sha256-tree-v2"
    assert single_evidence["sha256"] != split_evidence["sha256"]
    assert single_evidence["file_count"] == 1
    assert split_evidence["file_count"] == 2


def test_v2_digest_is_independent_of_creation_order(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    (first / "b.bin").write_bytes(b"beta")
    (first / "a.bin").write_bytes(b"alpha")
    (second / "a.bin").write_bytes(b"alpha")
    (second / "b.bin").write_bytes(b"beta")

    assert foundry_cache_tree_sha256(first) == foundry_cache_tree_sha256(second)


def test_v2_digest_binds_relative_path_and_file_bytes(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    model = cache / "model.bin"
    model.write_bytes(b"model-v1")

    original = foundry_cache_tree_sha256(cache)
    model.write_bytes(b"model-v2")
    changed_content = foundry_cache_tree_sha256(cache)
    model.rename(cache / "renamed.bin")
    changed_path = foundry_cache_tree_sha256(cache)

    assert original["sha256"] != changed_content["sha256"]
    assert changed_content["sha256"] != changed_path["sha256"]
    assert changed_path["file_count"] == 1
    assert changed_path["total_bytes"] == len(b"model-v2")


def test_empty_cache_tree_is_not_valid_model_checksum_evidence(tmp_path: Path) -> None:
    cache = tmp_path / "empty"
    cache.mkdir()

    with pytest.raises(ValueError, match="contains no regular files"):
        foundry_cache_tree_sha256(cache)


def test_cache_file_symlink_fails_closed(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip(
            "Windows symlink creation is privilege-dependent; reparse policy is tested directly"
        )

    cache = tmp_path / "cache"
    cache.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside-model-bytes")
    (cache / "model.bin").symlink_to(outside)

    with pytest.raises(ValueError, match="filesystem indirection"):
        foundry_cache_tree_sha256(cache)


def test_windows_reparse_attribute_fails_closed_without_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    actual = os.lstat(cache)
    reparse_flag = 0x0400

    fake = SimpleNamespace(
        st_mode=actual.st_mode,
        st_file_attributes=reparse_flag,
    )
    monkeypatch.setattr(
        foundry_cache_evidence.stat,
        "FILE_ATTRIBUTE_REPARSE_POINT",
        reparse_flag,
        raising=False,
    )
    monkeypatch.setattr(foundry_cache_evidence.os, "lstat", lambda _path: fake)
    monkeypatch.setattr(Path, "is_symlink", lambda _self: False)

    with pytest.raises(ValueError, match="filesystem indirection"):
        foundry_cache_tree_sha256(cache)


def test_walk_enumeration_error_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()

    def denied_walk(_root, *, topdown, onerror, followlinks):
        assert topdown is True
        assert followlinks is False
        onerror(PermissionError("denied"))
        yield (str(cache), [], [])

    monkeypatch.setattr(foundry_cache_evidence.os, "walk", denied_walk)

    with pytest.raises(ValueError, match="cannot be enumerated"):
        foundry_cache_tree_sha256(cache)


def test_added_file_during_hashing_invalidates_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "model.bin").write_bytes(b"model")
    original_cache_files = foundry_cache_evidence._cache_files
    inventory_calls = 0

    def changing_inventory(root: Path):
        nonlocal inventory_calls
        inventory_calls += 1
        if inventory_calls == 2:
            (root / "late.bin").write_bytes(b"late")
        return original_cache_files(root)

    monkeypatch.setattr(foundry_cache_evidence, "_cache_files", changing_inventory)

    with pytest.raises(ValueError, match="tree changed while hashing"):
        foundry_cache_tree_sha256(cache)
