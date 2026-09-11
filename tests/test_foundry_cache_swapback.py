from __future__ import annotations

import os
from pathlib import Path

import pytest

from nika_core.model_gateway import foundry_cache_evidence
from nika_core.model_gateway.foundry_cache_evidence import foundry_cache_tree_sha256


def test_same_size_swap_open_then_restore_original_path_fails_closed(
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

    original_backup = tmp_path / "original-backup.bin"
    opened_replacement = tmp_path / "opened-replacement.bin"
    original_open = foundry_cache_evidence.os.open
    swapped = False

    def swap_open_restore(path, flags):
        nonlocal swapped
        if Path(path) == model and not swapped:
            os.replace(model, original_backup)
            os.replace(replacement, model)
            descriptor = original_open(path, flags)
            os.replace(model, opened_replacement)
            os.replace(original_backup, model)
            swapped = True
            return descriptor
        return original_open(path, flags)

    monkeypatch.setattr(foundry_cache_evidence.os, "open", swap_open_restore)

    # POSIX permits renaming an open file, so the descriptor/path identity guard must
    # catch the completed swap-back. Windows can deny that rename while the descriptor
    # is open; in that case the injected attack is already stopped inside os.open and
    # the production boundary deliberately collapses the OSError to the bounded
    # fail-closed "cannot be opened" diagnostic.
    expected = "cannot be opened" if os.name == "nt" else "changed before hashing"
    with pytest.raises(ValueError, match=expected):
        foundry_cache_tree_sha256(cache)
