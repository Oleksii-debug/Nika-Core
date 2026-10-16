from __future__ import annotations

from pathlib import Path

import pytest

import nika_core.artifacts.registry as registry_module
from nika_core.artifacts import ArtifactRegistry, ArtifactRegistryError
from nika_core.data.sqlite import SQLiteStore


def test_register_file_binds_authorized_path_to_opened_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    source = allowed / "artifact.bin"
    foreign = outside / "artifact.bin"
    source.write_bytes(b"trusted-bytes")
    foreign.write_bytes(b"foreign-bytes")
    assert source.stat().st_size == foreign.stat().st_size

    registry = ArtifactRegistry.from_store(
        SQLiteStore(tmp_path / "state.sqlite3"),
        local_file_roots=(allowed,),
    )
    expected_source = source.resolve()
    real_open = registry_module.os.open

    def substituted_open(path: str | bytes | Path, flags: int, mode: int = 0o777) -> int:
        if Path(path) == expected_source:
            return real_open(foreign, flags, mode)
        return real_open(path, flags, mode)

    monkeypatch.setattr(registry_module.os, "open", substituted_open)

    with pytest.raises(ArtifactRegistryError, match="identity changed"):
        registry.register_file(
            workspace_id="workspace-a",
            idempotency_key="substituted-open",
            path=source,
            kind="evidence",
        )

    assert registry.list(workspace_id="workspace-a") == ()
