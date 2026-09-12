from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

import nika_core.training_artifacts.integrity as integrity
from nika_core.training_artifacts import (
    CandidateArtifactIntegrityError,
    ExpectedCandidateArtifact,
    VerifiedCandidateArtifact,
    verify_candidate_artifact,
)


def _expected(path: Path, *, artifact_ref: str = "models/candidate/job-1") -> ExpectedCandidateArtifact:
    data = path.read_bytes()
    return ExpectedCandidateArtifact(
        artifact_ref=artifact_ref,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
    )


def test_matching_regular_file_produces_minimized_evidence(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    candidate = root / "candidate.bin"
    candidate.write_bytes(b"candidate-model-weights")
    expected = _expected(candidate)

    verified = verify_candidate_artifact(candidate, expected, allowed_root=root)

    assert verified == VerifiedCandidateArtifact(
        artifact_ref=expected.artifact_ref,
        sha256=expected.sha256,
        size_bytes=expected.size_bytes,
    )
    assert not hasattr(verified, "path")


def test_digest_mismatch_fails_closed_without_exposing_path(tmp_path: Path) -> None:
    candidate = tmp_path / "private-model-name.bin"
    candidate.write_bytes(b"candidate")
    expected = ExpectedCandidateArtifact(
        artifact_ref="models/candidate/job-1",
        sha256="0" * 64,
        size_bytes=candidate.stat().st_size,
    )

    with pytest.raises(CandidateArtifactIntegrityError) as exc_info:
        verify_candidate_artifact(candidate, expected)

    assert "digest does not match" in str(exc_info.value)
    assert str(candidate) not in str(exc_info.value)


def test_expected_size_mismatch_fails_before_hashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "candidate.bin"
    candidate.write_bytes(b"123456")
    expected = ExpectedCandidateArtifact(
        artifact_ref="models/candidate/job-1",
        sha256=hashlib.sha256(b"123456").hexdigest(),
        size_bytes=5,
    )
    read_called = False
    original_read = integrity.os.read

    def observing_read(file_descriptor: int, size: int) -> bytes:
        nonlocal read_called
        read_called = True
        return original_read(file_descriptor, size)

    monkeypatch.setattr(integrity.os, "read", observing_read)

    with pytest.raises(CandidateArtifactIntegrityError, match="size does not match"):
        verify_candidate_artifact(candidate, expected)

    assert read_called is False


def test_directory_is_not_a_candidate_artifact(tmp_path: Path) -> None:
    expected = ExpectedCandidateArtifact(
        artifact_ref="models/candidate/job-1",
        sha256=hashlib.sha256(b"").hexdigest(),
        size_bytes=0,
    )

    with pytest.raises(CandidateArtifactIntegrityError, match="regular file"):
        verify_candidate_artifact(tmp_path, expected)


def test_final_symbolic_link_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "real.bin"
    target.write_bytes(b"candidate")
    link = tmp_path / "candidate.bin"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable in this environment")

    with pytest.raises(CandidateArtifactIntegrityError, match="symbolic link"):
        verify_candidate_artifact(link, _expected(target))


def test_candidate_outside_allowed_root_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"candidate")

    with pytest.raises(CandidateArtifactIntegrityError, match="outside the allowed root"):
        verify_candidate_artifact(outside, _expected(outside), allowed_root=root)


def test_nested_candidate_inside_allowed_root_passes(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    nested = root / "models" / "run-1"
    nested.mkdir(parents=True)
    candidate = nested / "candidate.bin"
    candidate.write_bytes(b"candidate")

    verified = verify_candidate_artifact(candidate, _expected(candidate), allowed_root=root)

    assert verified.size_bytes == len(b"candidate")


def test_relative_candidate_path_is_rejected() -> None:
    expected = ExpectedCandidateArtifact(
        artifact_ref="models/candidate/job-1",
        sha256="0" * 64,
        size_bytes=0,
    )

    with pytest.raises(ValueError, match="must be absolute"):
        verify_candidate_artifact(Path("candidate.bin"), expected)


@pytest.mark.parametrize(
    ("artifact_ref", "sha256", "size_bytes", "error_type"),
    [
        ("", "0" * 64, 0, ValueError),
        (" candidate ", "0" * 64, 0, ValueError),
        ("candidate\nname", "0" * 64, 0, ValueError),
        ("candidate", "A" * 64, 0, ValueError),
        ("candidate", "0" * 63, 0, ValueError),
        ("candidate", "0" * 64, -1, ValueError),
        ("candidate", "0" * 64, True, ValueError),
        ("candidate", "0" * 64, 1 << 63, ValueError),
        (1, "0" * 64, 0, TypeError),
    ],
)
def test_expected_artifact_contract_is_strict(
    artifact_ref: object,
    sha256: object,
    size_bytes: object,
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type):
        ExpectedCandidateArtifact(
            artifact_ref=artifact_ref,  # type: ignore[arg-type]
            sha256=sha256,  # type: ignore[arg-type]
            size_bytes=size_bytes,  # type: ignore[arg-type]
        )


def test_hashing_is_streamed_in_bounded_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "large.bin"
    candidate.write_bytes(b"x" * (integrity._READ_CHUNK_BYTES * 2 + 123))
    expected = _expected(candidate)
    requested_sizes: list[int] = []
    original_read = integrity.os.read

    def bounded_read(file_descriptor: int, size: int) -> bytes:
        requested_sizes.append(size)
        return original_read(file_descriptor, size)

    monkeypatch.setattr(integrity.os, "read", bounded_read)

    verify_candidate_artifact(candidate, expected)

    assert requested_sizes
    assert max(requested_sizes) <= integrity._READ_CHUNK_BYTES
    assert len(requested_sizes) >= 3


def test_growth_during_hashing_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "candidate.bin"
    candidate.write_bytes(b"a" * (integrity._READ_CHUNK_BYTES + 128))
    expected = _expected(candidate)
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
        verify_candidate_artifact(candidate, expected)


def test_metadata_change_during_hashing_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "candidate.bin"
    candidate.write_bytes(b"candidate")
    expected = _expected(candidate)
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
        verify_candidate_artifact(candidate, expected)


def test_expected_object_must_be_canonical_type(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.bin"
    candidate.write_bytes(b"candidate")

    with pytest.raises(TypeError, match="ExpectedCandidateArtifact"):
        verify_candidate_artifact(candidate, object())  # type: ignore[arg-type]
