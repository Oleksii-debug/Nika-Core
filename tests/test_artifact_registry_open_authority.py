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


def test_register_file_rejects_same_inode_rewrite_with_restored_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hashlib
    import os

    source = tmp_path / "Звіт зі пробілами.bin"
    chunk_size = 1024 * 1024
    original = b"A" * (2 * chunk_size)
    replacement = b"B" * chunk_size + b"C" * chunk_size
    phantom = b"A" * chunk_size + b"C" * chunk_size
    source.write_bytes(original)
    original_stat = source.stat()
    registry = ArtifactRegistry.from_store(
        SQLiteStore(tmp_path / "state.sqlite3"), local_file_roots=(tmp_path,),
    )
    real_fdopen = registry_module.os.fdopen
    observations: list[tuple[int, int]] = []

    class MutatingReader:
        def __init__(self, handle) -> None:
            self.handle = handle
            self.mutated = False

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def fileno(self):
            return self.handle.fileno()

        def read(self, size):
            value = self.handle.read(size)
            if value and not self.mutated:
                self.mutated = True
                before = os.fstat(self.handle.fileno())
                with source.open("r+b", buffering=0) as writer:
                    writer.write(replacement)
                    writer.flush()
                    os.fsync(writer.fileno())
                os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
                after = os.fstat(self.handle.fileno())
                assert (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) == (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                )
                observations.append((before.st_ctime_ns, after.st_ctime_ns))
            return value

    monkeypatch.setattr(
        registry_module.os, "fdopen",
        lambda *args, **kwargs: MutatingReader(real_fdopen(*args, **kwargs)),
    )
    with pytest.raises(ArtifactRegistryError, match="changed while it was being hashed"):
        registry.register_file(
            workspace_id="workspace-a", idempotency_key="same-inode-mutation",
            path=source, kind="evidence",
        )
    assert len(observations) == 1
    assert observations[0][0] != observations[0][1]
    assert source.read_bytes() == replacement
    assert hashlib.sha256(phantom).digest() != hashlib.sha256(replacement).digest()
    assert registry.list() == ()
