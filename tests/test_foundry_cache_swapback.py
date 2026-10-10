from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.model_gateway import foundry_cache_evidence
from nika_core.model_gateway.foundry_cache_evidence import foundry_cache_tree_sha256


def test_foreign_same_size_descriptor_is_rejected_before_hashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    model = cache / "model.bin"
    model.write_bytes(b"original")

    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"replaced")
    assert replacement.stat().st_size == model.stat().st_size

    original_open = foundry_cache_evidence.os.open
    redirected = False

    def open_foreign_descriptor(path, flags):
        nonlocal redirected
        if Path(path) == model and not redirected:
            redirected = True
            return original_open(replacement, flags)
        return original_open(path, flags)

    def fail_if_foreign_bytes_are_read(_descriptor, _length):
        pytest.fail("foreign descriptor bytes were read before identity rejection")

    monkeypatch.setattr(foundry_cache_evidence.os, "open", open_foreign_descriptor)
    monkeypatch.setattr(foundry_cache_evidence.os, "read", fail_if_foreign_bytes_are_read)

    with pytest.raises(ValueError, match="changed before hashing"):
        foundry_cache_tree_sha256(cache)

    assert redirected
