from __future__ import annotations

import hashlib
import hmac
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from nika_core.model_artifacts import ModelArtifactDescriptor, ModelIntegrityBasis

_READ_CHUNK_BYTES = 1024 * 1024


class CandidateArtifactIntegrityError(RuntimeError):
    """Safe failure from the candidate-model artifact integrity boundary."""


@dataclass(frozen=True, slots=True)
class VerifiedCandidateArtifact:
    """Minimized proof that physical bytes matched one canonical descriptor."""

    descriptor_digest: str
    registry_key: str
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
        raise CandidateArtifactIntegrityError(
            "candidate artifact could not be opened safely"
        ) from exc


def _require_physical_descriptor(descriptor: ModelArtifactDescriptor) -> tuple[str, int]:
    if type(descriptor) is not ModelArtifactDescriptor:
        raise TypeError("descriptor must be a ModelArtifactDescriptor")
    if descriptor.integrity_basis is not ModelIntegrityBasis.SHA256:
        raise CandidateArtifactIntegrityError(
            "candidate artifact requires canonical SHA-256 integrity provenance"
        )
    if descriptor.sha256 is None or descriptor.size_bytes is None:
        raise CandidateArtifactIntegrityError(
            "candidate artifact descriptor requires exact digest and size"
        )
    return descriptor.sha256, descriptor.size_bytes


def verify_candidate_artifact(
    path: str | os.PathLike[str],
    descriptor: ModelArtifactDescriptor,
    *,
    allowed_root: str | os.PathLike[str] | None = None,
) -> VerifiedCandidateArtifact:
    """Verify physical candidate bytes against canonical model provenance.

    The canonical descriptor is owned by ``nika_core.model_artifacts``. This adapter
    owns only physical byte verification. It hashes one opened descriptor in bounded
    reads and requires pre-open, opened, post-read and final path identity to agree.
    Paths and model bytes are deliberately absent from returned evidence.
    """
    expected_sha256, expected_size = _require_physical_descriptor(descriptor)

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
            raise CandidateArtifactIntegrityError(
                "candidate artifact changed before verification"
            )
        if opened.st_size != expected_size:
            raise CandidateArtifactIntegrityError(
                "candidate artifact size does not match provenance"
            )

        digest = hashlib.sha256()
        total_bytes = 0
        while True:
            remaining_with_sentinel = expected_size + 1 - total_bytes
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
            if total_bytes > expected_size:
                raise CandidateArtifactIntegrityError(
                    "candidate artifact grew during verification"
                )
            digest.update(chunk)

        if total_bytes != expected_size:
            raise CandidateArtifactIntegrityError(
                "candidate artifact size changed during verification"
            )
        try:
            after_open = os.fstat(file_descriptor)
        except OSError as exc:
            raise CandidateArtifactIntegrityError(
                "candidate artifact metadata could not be re-read"
            ) from exc
        if _stat_identity(after_open) != opened_identity:
            raise CandidateArtifactIntegrityError(
                "candidate artifact changed during verification"
            )
        actual_sha256 = digest.hexdigest()
    finally:
        try:
            os.close(file_descriptor)
        except OSError:
            pass

    after_path = _safe_lstat(candidate)
    _require_regular(after_path)
    if _stat_identity(after_path) != before_identity:
        raise CandidateArtifactIntegrityError(
            "candidate artifact path changed during verification"
        )
    if not hmac.compare_digest(actual_sha256, expected_sha256):
        raise CandidateArtifactIntegrityError(
            "candidate artifact digest does not match provenance"
        )

    return VerifiedCandidateArtifact(
        descriptor_digest=descriptor.descriptor_digest,
        registry_key=descriptor.registry_key,
        sha256=expected_sha256,
        size_bytes=expected_size,
    )
