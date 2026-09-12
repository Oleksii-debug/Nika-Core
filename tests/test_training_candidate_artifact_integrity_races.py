from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from nika_core.model_artifacts import (
    ModelArtifactDescriptor,
    ModelArtifactKind,
    ModelIntegrityBasis,
)
from nika_core.training_artifacts import (
    CandidateArtifactIntegrityError,
    integrity,
    verify_candidate_artifact,
)


def _descriptor(path: Path) -> ModelArtifactDescriptor:
    payload = path.read_bytes()
    return ModelArtifactDescriptor(
        kind=ModelArtifactKind.EMBEDDED,
        provider_id="training-runtime",
        model_id="candidate-race",
        model_version="1",
        source_reference="https://models.example.test/candidate-race",
        license_reference="https://licenses.example.test/candidate-race",
        integrity_basis=ModelIntegrityBasis.SHA256,
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        capabilities=("text",),
    )


def test_symbolic_link_allowed_root_is_rejected(tmp_path: Path) -> None:
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    candidate = real_root / "candidate.bin"
    candidate.write_bytes(b"candidate")
    linked_root = tmp_path / "linked-root"
    try:
        linked_root.symlink_to(real_root, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable in this environment")

    with pytest.raises(CandidateArtifactIntegrityError, match="allowed_root.*link"):
        verify_candidate_artifact(candidate, _descriptor(candidate), allowed_root=linked_root)


def test_symlinked_parent_cannot_escape_allowed_root(tmp_path: Path) -> None:
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    candidate = outside / "candidate.bin"
    candidate.write_bytes(b"candidate")
    escape = allowed_root / "escape"
    try:
        escape.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable in this environment")

    with pytest.raises(CandidateArtifactIntegrityError, match="outside the allowed root"):
        verify_candidate_artifact(
            escape / "candidate.bin",
            _descriptor(candidate),
            allowed_root=allowed_root,
        )


def test_parent_swap_after_containment_check_cannot_escape_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    inner = allowed_root / "inner"
    inner.mkdir()
    candidate = inner / "candidate.bin"
    candidate.write_bytes(b"same-trusted-bytes")
    descriptor = _descriptor(candidate)

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "candidate.bin").write_bytes(b"same-trusted-bytes")
    displaced = allowed_root / "inner-original"
    original_open = integrity._open_contained_read_only
    swapped = False

    def swapping_open(path: Path, root: Path) -> tuple[int, os.stat_result]:
        nonlocal swapped
        if not swapped:
            swapped = True
            inner.rename(displaced)
            try:
                inner.symlink_to(outside, target_is_directory=True)
            except OSError:
                pytest.skip("symbolic links are unavailable in this environment")
        return original_open(path, root)

    monkeypatch.setattr(integrity, "_open_contained_read_only", swapping_open)

    with pytest.raises(
        CandidateArtifactIntegrityError,
        match="allowed root|opened safely within",
    ):
        verify_candidate_artifact(candidate, descriptor, allowed_root=allowed_root)


def test_replacement_between_lstat_and_open_fails_before_hashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "candidate.bin"
    candidate.write_bytes(b"original-bytes")
    descriptor = _descriptor(candidate)
    displaced = tmp_path / "displaced.bin"
    original_open = integrity._open_read_only
    replaced = False

    def replacing_open(path: Path) -> int:
        nonlocal replaced
        if not replaced:
            replaced = True
            candidate.rename(displaced)
            candidate.write_bytes(b"replacement-xx")
        return original_open(path)

    monkeypatch.setattr(integrity, "_open_read_only", replacing_open)

    with pytest.raises(CandidateArtifactIntegrityError, match="changed before verification"):
        verify_candidate_artifact(candidate, descriptor)


def test_path_replacement_after_hashing_fails_final_identity_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "candidate.bin"
    candidate.write_bytes(b"original-bytes")
    descriptor = _descriptor(candidate)
    displaced = tmp_path / "displaced.bin"
    original_close = os.close
    replaced = False

    def replacing_close(file_descriptor: int) -> None:
        nonlocal replaced
        original_close(file_descriptor)
        if not replaced:
            replaced = True
            candidate.rename(displaced)
            candidate.write_bytes(b"replacement-xx")

    monkeypatch.setattr(integrity.os, "close", replacing_close)

    with pytest.raises(CandidateArtifactIntegrityError, match="path changed"):
        verify_candidate_artifact(candidate, descriptor)
