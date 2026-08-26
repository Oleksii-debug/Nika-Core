from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from nika_core.model_gateway import foundry_cache_evidence
from nika_core.model_gateway.foundry_cache_evidence import foundry_cache_tree_sha256


def test_path_and_descriptor_ctime_domains_do_not_false_reject_unchanged_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "model.bin").write_bytes(b"model")

    original_fstat = foundry_cache_evidence.os.fstat

    def descriptor_stat(descriptor: int):
        info = original_fstat(descriptor)
        return SimpleNamespace(
            st_mode=info.st_mode,
            st_dev=info.st_dev,
            st_ino=info.st_ino,
            st_size=info.st_size,
            st_mtime_ns=info.st_mtime_ns,
            st_ctime_ns=info.st_ctime_ns + 1,
        )

    monkeypatch.setattr(foundry_cache_evidence.os, "fstat", descriptor_stat)

    evidence = foundry_cache_tree_sha256(cache)

    assert evidence["algorithm"] == "sha256-tree-v2"
    assert evidence["file_count"] == 1
    assert evidence["total_bytes"] == len(b"model")


def test_pathname_replacement_is_still_rejected_after_stat_domain_split(
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
