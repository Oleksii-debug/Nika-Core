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


def test_cache_directory_symlink_path_escape_fails_closed(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip(
            "Windows symlink creation is privilege-dependent; reparse policy is tested directly"
        )

    cache = tmp_path / "cache"
    outside = tmp_path / "outside"
    cache.mkdir()
    outside.mkdir()
    (outside / "model.bin").write_bytes(b"outside-model-bytes")
    (cache / "redirect").symlink_to(outside, target_is_directory=True)

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


def test_walk_entry_outside_selected_root_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "cache"
    outside = tmp_path / "outside"
    cache.mkdir()
    outside.mkdir()
    (outside / "model.bin").write_bytes(b"outside")

    def escaped_walk(_root, *, topdown, onerror, followlinks):
        assert topdown is True
        assert onerror is not None
        assert followlinks is False
        yield (str(outside), [], ["model.bin"])

    monkeypatch.setattr(foundry_cache_evidence.os, "walk", escaped_walk)

    with pytest.raises(ValueError, match="escapes selected root"):
        foundry_cache_tree_sha256(cache)


def test_file_replacement_between_inventory_and_open_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    model = cache / "model.bin"
    model.write_bytes(b"original")
    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"replaced")

    original_open = foundry_cache_evidence.os.open
    replaced = False

    def replacing_open(path, flags):
        nonlocal replaced
        if Path(path) == model and not replaced:
            os.replace(replacement, model)
            replaced = True
        return original_open(path, flags)

    monkeypatch.setattr(foundry_cache_evidence.os, "open", replacing_open)

    with pytest.raises(ValueError, match="changed before hashing"):
        foundry_cache_tree_sha256(cache)


def test_open_file_metadata_change_during_hashing_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "model.bin").write_bytes(b"model")

    original_fstat = foundry_cache_evidence.os.fstat
    calls = 0

    def changed_fstat(descriptor):
        nonlocal calls
        calls += 1
        info = original_fstat(descriptor)
        if calls != 2:
            return info
        return SimpleNamespace(
            st_mode=info.st_mode,
            st_dev=info.st_dev,
            st_ino=info.st_ino,
            st_size=info.st_size,
            st_mtime_ns=info.st_mtime_ns + 1,
            st_ctime_ns=info.st_ctime_ns,
        )

    monkeypatch.setattr(foundry_cache_evidence.os, "fstat", changed_fstat)

    with pytest.raises(ValueError, match="changed while hashing"):
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


def test_removed_file_during_hashing_invalidates_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "model.bin").write_bytes(b"model")
    late = cache / "second.bin"
    late.write_bytes(b"second")
    original_cache_files = foundry_cache_evidence._cache_files
    inventory_calls = 0

    def changing_inventory(root: Path):
        nonlocal inventory_calls
        inventory_calls += 1
        if inventory_calls == 2:
            late.unlink()
        return original_cache_files(root)

    monkeypatch.setattr(foundry_cache_evidence, "_cache_files", changing_inventory)

    with pytest.raises(ValueError, match="tree changed while hashing"):
        foundry_cache_tree_sha256(cache)
