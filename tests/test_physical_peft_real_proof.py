from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def _proof_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "physical_peft_real_proof.py"
    spec = importlib.util.spec_from_file_location("physical_peft_real_proof_test_target", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stable_file_bytes_round_trip(tmp_path: Path) -> None:
    proof = _proof_module()
    target = tmp_path / "candidate.bin"
    target.write_bytes(b"candidate-bytes")

    assert proof._stable_file_bytes(
        target,
        max_bytes=1024,
        name="candidate",
    ) == b"candidate-bytes"


def test_stable_file_bytes_rejects_bound(tmp_path: Path) -> None:
    proof = _proof_module()
    target = tmp_path / "candidate.bin"
    target.write_bytes(b"candidate-bytes")

    with pytest.raises(proof.ProofError, match="size or file type is invalid"):
        proof._stable_file_bytes(target, max_bytes=4, name="candidate")


def test_stable_file_bytes_rejects_post_read_identity_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = _proof_module()
    target = tmp_path / "candidate.bin"
    target.write_bytes(b"candidate-bytes")
    original_lstat = proof.os.lstat
    initial = original_lstat(target)
    calls = 0

    def changed_lstat(path: object):
        nonlocal calls
        value = original_lstat(path)
        if Path(path) != target:
            return value
        calls += 1
        if calls == 1:
            return value
        return SimpleNamespace(
            st_mode=initial.st_mode,
            st_dev=initial.st_dev,
            st_ino=initial.st_ino,
            st_size=initial.st_size,
            st_mtime_ns=initial.st_mtime_ns + 1,
            st_file_attributes=getattr(initial, "st_file_attributes", 0),
        )

    monkeypatch.setattr(proof.os, "lstat", changed_lstat)

    with pytest.raises(proof.ProofError, match="changed while it was being snapshotted"):
        proof._stable_file_bytes(target, max_bytes=1024, name="candidate")
