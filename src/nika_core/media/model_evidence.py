from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from nika_core.media.contracts import ModelDescriptor
from nika_core.media.errors import MediaError, MediaErrorCode

_MODEL_BUNDLE_SCHEMA = b"nika-media-local-model-v1\0"
_READ_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class LocalModelEvidence:
    """Deterministic evidence for an explicitly selected local model bundle."""

    size_bytes: int
    file_count: int
    sha256: str | None


def inspect_model_directory(
    root: Path,
    *,
    compute_sha256: bool,
) -> LocalModelEvidence:
    """Inspect a local model directory without following filesystem indirection."""

    resolved = _require_local_directory(root)
    entries: list[tuple[str, Path]] = []
    total_size = 0

    for current, dirnames, filenames in os.walk(resolved, topdown=True, followlinks=False):
        current_path = Path(current)
        dirnames.sort()
        filenames.sort()

        for dirname in tuple(dirnames):
            child = current_path / dirname
            _reject_filesystem_indirection(child, label="model directory")
        for filename in filenames:
            child = current_path / filename
            _reject_filesystem_indirection(child, label="model file")
            if not child.is_file():
                raise MediaError(
                    MediaErrorCode.INVALID_SOURCE,
                    f"model bundle entry is not a regular file: {child.name}",
                )
            relative = child.relative_to(resolved).as_posix()
            entries.append((relative, child))
            total_size += child.stat().st_size

    if not entries:
        raise MediaError(
            MediaErrorCode.COMPONENT_MISSING,
            "local model directory contains no files",
        )

    digest = _hash_entries(entries) if compute_sha256 else None
    return LocalModelEvidence(
        size_bytes=total_size,
        file_count=len(entries),
        sha256=digest,
    )


def inspect_model_files(
    files: Mapping[str, Path],
    *,
    compute_sha256: bool,
) -> LocalModelEvidence:
    """Inspect explicit model files keyed by stable semantic roles."""

    if not files:
        raise ValueError("model file mapping must not be empty")

    entries: list[tuple[str, Path]] = []
    total_size = 0
    for role in sorted(files):
        normalized_role = role.strip()
        if not normalized_role or normalized_role != role:
            raise ValueError("model file role must be non-empty and normalized")
        path = files[role]
        _reject_filesystem_indirection(path, label=f"{role} model file")
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise MediaError(
                MediaErrorCode.COMPONENT_MISSING,
                f"{role} model file is missing",
            ) from exc
        if not resolved.is_file():
            raise MediaError(
                MediaErrorCode.COMPONENT_MISSING,
                f"{role} model file must be an existing local file",
            )
        entries.append((role, resolved))
        total_size += resolved.stat().st_size

    digest = _hash_entries(entries) if compute_sha256 else None
    return LocalModelEvidence(
        size_bytes=total_size,
        file_count=len(entries),
        sha256=digest,
    )


def bind_model_evidence(
    model: ModelDescriptor,
    evidence: LocalModelEvidence,
) -> ModelDescriptor:
    """Validate declared local identity and fill deterministic observed size."""

    if not model.license_reference.strip():
        raise ValueError("model license_reference must be non-empty")
    if model.size_bytes is not None and model.size_bytes != evidence.size_bytes:
        raise MediaError(
            MediaErrorCode.CHECKSUM_MISMATCH,
            "local model size does not match the approved model descriptor",
        )
    if model.sha256 is not None:
        if evidence.sha256 is None:
            raise ValueError("model sha256 validation requires computed checksum evidence")
        if model.sha256 != evidence.sha256:
            raise MediaError(
                MediaErrorCode.CHECKSUM_MISMATCH,
                "local model checksum does not match the approved model descriptor",
            )
    return model.model_copy(update={"size_bytes": evidence.size_bytes})


def _require_local_directory(path: Path) -> Path:
    _reject_filesystem_indirection(path, label="model directory")
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise MediaError(
            MediaErrorCode.COMPONENT_MISSING,
            "local model directory is missing",
        ) from exc
    if not resolved.is_dir():
        raise MediaError(
            MediaErrorCode.COMPONENT_MISSING,
            "local model path must be an existing directory",
        )
    return resolved


def _reject_filesystem_indirection(path: Path, *, label: str) -> None:
    if path.is_symlink():
        raise MediaError(
            MediaErrorCode.PATH_ESCAPE,
            f"{label} must not be a symbolic link",
        )
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        raise MediaError(
            MediaErrorCode.PATH_ESCAPE,
            f"{label} must not be a junction",
        )


def _hash_entries(entries: list[tuple[str, Path]]) -> str:
    digest = hashlib.sha256()
    digest.update(_MODEL_BUNDLE_SCHEMA)
    for identity, path in entries:
        encoded_identity = identity.encode("utf-8")
        size = path.stat().st_size
        digest.update(len(encoded_identity).to_bytes(4, "big"))
        digest.update(encoded_identity)
        digest.update(size.to_bytes(8, "big"))
        with path.open("rb") as handle:
            while chunk := handle.read(_READ_CHUNK_BYTES):
                digest.update(chunk)
    return digest.hexdigest()
