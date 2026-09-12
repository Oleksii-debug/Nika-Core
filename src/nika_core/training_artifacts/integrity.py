from __future__ import annotations

import hashlib
import hmac
import os
import stat
from dataclasses import dataclass
from pathlib import Path

_MAX_ARTIFACT_REF_BYTES = 4096
_MAX_SIZE_BYTES = (1 << 63) - 1
_READ_CHUNK_BYTES = 1024 * 1024
_HEX_DIGITS = frozenset("0123456789abcdef")


class CandidateArtifactIntegrityError(RuntimeError):
    """Safe failure from the candidate-model artifact integrity boundary."""


@dataclass(frozen=True, slots=True)
class ExpectedCandidateArtifact:
    """Trusted identity expected from bounded training evidence."""

    artifact_ref: str
    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        if type(self.artifact_ref) is not str:
            raise TypeError("artifact_ref must be text")
        if not self.artifact_ref or self.artifact_ref != self.artifact_ref.strip():
            raise ValueError("artifact_ref must be non-empty without surrounding whitespace")
        if any(ord(character) < 32 or ord(character) == 127 for character in self.artifact_ref):
            raise ValueError("artifact_ref must not contain control characters")
        try:
            encoded_ref = self.artifact_ref.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("artifact_ref must be valid UTF-8 text") from exc
        if len(encoded_ref) > _MAX_ARTIFACT_REF_BYTES:
            raise ValueError("artifact_ref exceeds the configured byte limit")
        if (
            type(self.sha256) is not str
            or len(self.sha256) != 64
            or any(character not in _HEX_DIGITS for character in self.sha256)
        ):
            raise ValueError("sha256 must be a lowercase 64-character digest")
        if (
            type(self.size_bytes) is not int
            or self.size_bytes < 0
            or self.size_bytes > _MAX_SIZE_BYTES
        ):
            raise ValueError("size_bytes must be an integer from 0 through signed 64-bit max")


@dataclass(frozen=True, slots=True)
class VerifiedCandidateArtifact:
    """Minimized evidence that one stable physical artifact matched trusted identity."""

    artifact_ref: str
    sha256: str
    size_bytes: int


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _require_regular(value: os.stat_result) -> None:
    if stat.S_ISLNK(value.st_mode):
        raise CandidateArtifactIntegrityError("candidate artifact must not be a symbolic link")
    if not stat.S_ISREG(value.st_mode):
        raise CandidateArtifactIntegrityError("candidate artifact must be a regular file")


def _safe_lstat(path: Path) -> os.stat_result:
    try:
        return os.lstat(path)
    except OSError as exc:
        raise CandidateArtifactIntegrityError("candidate artifact is not accessible") from exc


def _resolve_candidate_path(
    path: str | os.PathLike[str],
    *,
    allowed_root: str | os.PathLike[str] | None,
) -> Path:
    if isinstance(path, bytes):
        raise TypeError("candidate artifact path must be text or a text path-like object")
    candidate = Path(path)
    if not candidate.is_absolute():
        raise ValueError("candidate artifact path must be absolute")

    try:
        resolved_parent = candidate.parent.resolve(strict=True)
    except OSError as exc:
        raise CandidateArtifactIntegrityError("candidate artifact parent is not accessible") from exc
    resolved_candidate = resolved_parent / candidate.name

    if allowed_root is None:
        return resolved_candidate
    if isinstance(allowed_root, bytes):
        raise TypeError("allowed_root must be text or a text path-like object")
    root = Path(allowed_root)
    if not root.is_absolute():
        raise ValueError("allowed_root must be absolute")

    root_lstat = _safe_lstat(root)
    if stat.S_ISLNK(root_lstat.st_mode):
        raise CandidateArtifactIntegrityError("allowed_root must not be a symbolic link")
    if not stat.S_ISDIR(root_lstat.st_mode):
        raise CandidateArtifactIntegrityError("allowed_root must be a directory")
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise CandidateArtifactIntegrityError("allowed_root is not accessible") from exc
    if not resolved_parent.is_relative_to(resolved_root):
        raise CandidateArtifactIntegrityError("candidate artifact is outside the allowed root")
    return resolved_candidate


def _open_read_only(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags)
    except OSError as exc:
        raise CandidateArtifactIntegrityError("candidate artifact could not be opened safely") from exc


def verify_candidate_artifact(
    path: str | os.PathLike[str],
    expected: ExpectedCandidateArtifact,
    *,
    allowed_root: str | os.PathLike[str] | None = None,
) -> VerifiedCandidateArtifact:
    """Verify one immutable training candidate before it may enter evaluation.

    The file is hashed incrementally from one opened descriptor. A pre-open lstat,
    descriptor fstat, post-read fstat and final lstat must agree, preventing ordinary
    path replacement or in-place mutation from being credited as stable evidence.
    Physical paths and file contents are intentionally absent from returned evidence.
    """
    if type(expected) is not ExpectedCandidateArtifact:
        raise TypeError("expected must be an ExpectedCandidateArtifact")

    candidate = _resolve_candidate_path(path, allowed_root=allowed_root)
    before_path = _safe_lstat(candidate)
    _require_regular(before_path)
    before_identity = _stat_identity(before_path)

    file_descriptor = _open_read_only(candidate)
    try:
        try:
            opened = os.fstat(file_descriptor)
        except OSError as exc:
            raise CandidateArtifactIntegrityError(
                "candidate artifact metadata could not be read"
            ) from exc
        _require_regular(opened)
        opened_identity = _stat_identity(opened)
        if opened_identity != before_identity:
            raise CandidateArtifactIntegrityError("candidate artifact changed before verification")
        if opened.st_size != expected.size_bytes:
            raise CandidateArtifactIntegrityError("candidate artifact size does not match evidence")

        digest = hashlib.sha256()
        total_bytes = 0
        while True:
            remaining_with_sentinel = expected.size_bytes + 1 - total_bytes
            if remaining_with_sentinel <= 0:
                raise CandidateArtifactIntegrityError(
                    "candidate artifact grew during verification"
                )
            try:
                chunk = os.read(
                    file_descriptor,
                    min(_READ_CHUNK_BYTES, remaining_with_sentinel),
                )
            except OSError as exc:
                raise CandidateArtifactIntegrityError(
                    "candidate artifact could not be read"
                ) from exc
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > expected.size_bytes:
                raise CandidateArtifactIntegrityError(
                    "candidate artifact grew during verification"
                )
            digest.update(chunk)

        if total_bytes != expected.size_bytes:
            raise CandidateArtifactIntegrityError("candidate artifact size changed during verification")
        try:
            after_open = os.fstat(file_descriptor)
        except OSError as exc:
            raise CandidateArtifactIntegrityError(
                "candidate artifact metadata could not be re-read"
            ) from exc
        if _stat_identity(after_open) != opened_identity:
            raise CandidateArtifactIntegrityError("candidate artifact changed during verification")
        actual_sha256 = digest.hexdigest()
    finally:
        try:
            os.close(file_descriptor)
        except OSError:
            pass

    after_path = _safe_lstat(candidate)
    _require_regular(after_path)
    if _stat_identity(after_path) != before_identity:
        raise CandidateArtifactIntegrityError("candidate artifact path changed during verification")
    if not hmac.compare_digest(actual_sha256, expected.sha256):
        raise CandidateArtifactIntegrityError("candidate artifact digest does not match evidence")

    return VerifiedCandidateArtifact(
        artifact_ref=expected.artifact_ref,
        sha256=expected.sha256,
        size_bytes=expected.size_bytes,
    )
