from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path

import pytest

from nika_core.model_artifacts import (
    ModelArtifactDescriptor,
    ModelArtifactKind,
    ModelIntegrityBasis,
)
from nika_core.training_artifacts import (
    CandidateArtifactIntegrityError,
    VerifiedCandidateArtifact,
    integrity,
    verify_candidate_artifact,
)


def _descriptor(
    path: Path,
    *,
    model_id: str = "candidate-job-1",
    sha256: str | None = None,
    size_bytes: int | None = None,
) -> ModelArtifactDescriptor:
    data = path.read_bytes()
    return ModelArtifactDescriptor(
        kind=ModelArtifactKind.EMBEDDED,
        provider_id="training-runtime",
        model_id=model_id,
        model_version="candidate-1",
        source_reference="https://models.example.test/training/candidate",
        license_reference="https://licenses.example.test/training/candidate",
        integrity_basis=ModelIntegrityBasis.SHA256,
        sha256=sha256 if sha256 is not None else hashlib.sha256(data).hexdigest(),
        size_bytes=size_bytes if size_bytes is not None else len(data),
        capabilities=("text",),
    )


def test_matching_regular_file_produces_descriptor_bound_minimized_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    candidate = root / "candidate.bin"
    candidate.write_bytes(b"candidate-model-weights")
    descriptor = _descriptor(candidate)

    verified = verify_candidate_artifact(candidate, descriptor, allowed_root=root)

    assert verified == VerifiedCandidateArtifact(
        descriptor_digest=descriptor.descriptor_digest,
        registry_key=descriptor.registry_key,
        sha256=descriptor.sha256,
        size_bytes=descriptor.size_bytes,
    )
    assert not hasattr(verified, "path")
    assert not hasattr(verified, "source_reference")
    assert not hasattr(verified, "license_reference")


def test_same_bytes_under_different_canonical_identity_get_distinct_evidence(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate.bin"
    candidate.write_bytes(b"candidate-model-weights")
    first = _descriptor(candidate, model_id="candidate-job-1")
    second = _descriptor(candidate, model_id="candidate-job-2")

    first_evidence = verify_candidate_artifact(candidate, first)
    second_evidence = verify_candidate_artifact(candidate, second)

    assert first_evidence.sha256 == second_evidence.sha256
    assert first_evidence.descriptor_digest != second_evidence.descriptor_digest
    assert first_evidence.registry_key != second_evidence.registry_key


def test_digest_mismatch_fails_closed_without_exposing_path(tmp_path: Path) -> None:
    candidate = tmp_path / "private-model-name.bin"
    candidate.write_bytes(b"candidate")
    descriptor = _descriptor(candidate, sha256="0" * 64)

    with pytest.raises(CandidateArtifactIntegrityError) as exc_info:
        verify_candidate_artifact(candidate, descriptor)

    assert "digest does not match" in str(exc_info.value)
    assert str(candidate) not in str(exc_info.value)


def test_expected_size_mismatch_fails_before_hashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "candidate.bin"
    candidate.write_bytes(b"123456")
    descriptor = _descriptor(candidate, size_bytes=5)
    read_called = False
    original_read = integrity.os.read

    def observing_read(file_descriptor: int, size: int) -> bytes:
        nonlocal read_called
        read_called = True
        return original_read(file_descriptor, size)

    monkeypatch.setattr(integrity.os, "read", observing_read)

    with pytest.raises(CandidateArtifactIntegrityError, match="size does not match"):
        verify_candidate_artifact(candidate, descriptor)

    assert read_called is False


def test_directory_is_not_a_candidate_artifact(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"candidate")
    descriptor = _descriptor(source)

    with pytest.raises(CandidateArtifactIntegrityError, match="regular file"):
        verify_candidate_artifact(tmp_path, descriptor)


def test_final_symbolic_link_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "real.bin"
    target.write_bytes(b"candidate")
    link = tmp_path / "candidate.bin"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable in this environment")

    with pytest.raises(CandidateArtifactIntegrityError, match="symbolic link"):
        verify_candidate_artifact(link, _descriptor(target))


def test_candidate_outside_allowed_root_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"candidate")

    with pytest.raises(CandidateArtifactIntegrityError, match="outside the allowed root"):
        verify_candidate_artifact(outside, _descriptor(outside), allowed_root=root)


def test_nested_candidate_inside_allowed_root_passes(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    nested = root / "models" / "run-1"
    nested.mkdir(parents=True)
    candidate = nested / "candidate.bin"
    candidate.write_bytes(b"candidate")

    verified = verify_candidate_artifact(
        candidate,
        _descriptor(candidate),
        allowed_root=root,
    )

    assert verified.size_bytes == len(b"candidate")


def test_relative_candidate_path_is_rejected(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.bin"
    candidate.write_bytes(b"candidate")

    with pytest.raises(ValueError, match="must be absolute"):
        verify_candidate_artifact(Path("candidate.bin"), _descriptor(candidate))


def test_provider_identity_descriptor_is_not_physical_byte_authority(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.bin"
    candidate.write_bytes(b"candidate")
    descriptor = replace(
        _descriptor(candidate),
        integrity_basis=ModelIntegrityBasis.PROVIDER_IDENTITY,
        sha256=None,
    )

    with pytest.raises(CandidateArtifactIntegrityError, match="canonical SHA-256"):
        verify_candidate_artifact(candidate, descriptor)


def test_sha_descriptor_without_exact_size_is_rejected_before_file_access(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate.bin"
    candidate.write_bytes(b"candidate")
    descriptor = replace(_descriptor(candidate), size_bytes=None)
    candidate.unlink()

    with pytest.raises(CandidateArtifactIntegrityError, match="exact digest and size"):
        verify_candidate_artifact(candidate, descriptor)


def test_descriptor_object_must_be_canonical_type(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.bin"
    candidate.write_bytes(b"candidate")

    with pytest.raises(TypeError, match="ModelArtifactDescriptor"):
        verify_candidate_artifact(candidate, object())  # type: ignore[arg-type]


def test_hashing_is_streamed_in_bounded_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "large.bin"
    candidate.write_bytes(b"x" * (integrity._READ_CHUNK_BYTES * 2 + 123))
    descriptor = _descriptor(candidate)
    requested_sizes: list[int] = []
    original_read = integrity.os.read

    def bounded_read(file_descriptor: int, size: int) -> bytes:
        requested_sizes.append(size)
        return original_read(file_descriptor, size)

    monkeypatch.setattr(integrity.os, "read", bounded_read)

    verify_candidate_artifact(candidate, descriptor)

    assert requested_sizes
    assert max(requested_sizes) <= integrity._READ_CHUNK_BYTES
    assert len(requested_sizes) >= 3


def test_growth_during_hashing_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "candidate.bin"
    candidate.write_bytes(b"a" * (integrity._READ_CHUNK_BYTES + 128))
    descriptor = _descriptor(candidate)
    original_read = integrity.os.read
    mutated = False

    def mutating_read(file_descriptor: int, size: int) -> bytes:
        nonlocal mutated
        chunk = original_read(file_descriptor, size)
        if chunk and not mutated:
            mutated = True
            with candidate.open("ab") as stream:
                stream.write(b"extra")
        return chunk

    monkeypatch.setattr(integrity.os, "read", mutating_read)

    with pytest.raises(CandidateArtifactIntegrityError, match="grew during verification"):
        verify_candidate_artifact(candidate, descriptor)


def test_metadata_change_during_hashing_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "candidate.bin"
    candidate.write_bytes(b"candidate")
    descriptor = _descriptor(candidate)
    original_fstat = integrity.os.fstat
    calls = 0

    def unstable_fstat(file_descriptor: int) -> os.stat_result:
        nonlocal calls
        value = original_fstat(file_descriptor)
        calls += 1
        if calls == 1:
            return value
        fields = list(value)
        fields[8] = value.st_mtime + 10
        return os.stat_result(fields)

    monkeypatch.setattr(integrity.os, "fstat", unstable_fstat)

    with pytest.raises(CandidateArtifactIntegrityError, match="changed during verification"):
        verify_candidate_artifact(candidate, descriptor)
