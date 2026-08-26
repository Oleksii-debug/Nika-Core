from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

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


@dataclass(slots=True)
class LocalModelRuntimeSnapshot:
    """Private runtime copy whose bytes are covered by checksum evidence."""

    root: Path
    evidence: LocalModelEvidence
    _owner: TemporaryDirectory[str] = field(repr=False, compare=False)
    role_paths: tuple[tuple[str, Path], ...] = ()

    def path_for(self, role: str) -> Path:
        for candidate_role, path in self.role_paths:
            if candidate_role == role:
                return path
        raise KeyError(role)


def inspect_model_directory(
    root: Path,
    *,
    compute_sha256: bool,
) -> LocalModelEvidence:
    """Inspect a local model directory without following filesystem indirection."""

    resolved = _require_local_directory(root)
    entries = _collect_directory_entries(resolved)
    return _evidence_for_entries(entries, compute_sha256=compute_sha256)


def inspect_model_files(
    files: Mapping[str, Path],
    *,
    compute_sha256: bool,
) -> LocalModelEvidence:
    """Inspect explicit model files keyed by stable semantic roles."""

    entries = _collect_role_entries(files)
    return _evidence_for_entries(entries, compute_sha256=compute_sha256)


def snapshot_model_directory(root: Path) -> LocalModelRuntimeSnapshot:
    """Copy a model directory to a private runtime-owned checksum snapshot."""

    resolved = _require_local_directory(root)
    source_entries = _collect_directory_entries(resolved)
    owner = TemporaryDirectory(prefix="nika-asr-model-")
    snapshot_root = Path(owner.name) / "bundle"
    snapshot_root.mkdir()

    snapshot_entries: list[tuple[str, Path]] = []
    for identity, source in source_entries:
        _reject_filesystem_indirection(source, label="model file")
        if not source.is_file():
            raise MediaError(
                MediaErrorCode.INVALID_SOURCE,
                f"model bundle entry changed before runtime snapshot: {source.name}",
            )
        destination = snapshot_root / Path(identity)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _copy_regular_file(source, destination)
        snapshot_entries.append((identity, destination))

    evidence = _evidence_for_entries(snapshot_entries, compute_sha256=True)
    return LocalModelRuntimeSnapshot(
        root=snapshot_root,
        evidence=evidence,
        _owner=owner,
    )


def snapshot_model_files(files: Mapping[str, Path]) -> LocalModelRuntimeSnapshot:
    """Copy explicit model files to a private runtime-owned checksum snapshot."""

    source_entries = _collect_role_entries(files)
    owner = TemporaryDirectory(prefix="nika-asr-model-")
    snapshot_root = Path(owner.name) / "bundle"
    snapshot_root.mkdir()

    role_paths: list[tuple[str, Path]] = []
    snapshot_entries: list[tuple[str, Path]] = []
    for index, (role, source) in enumerate(source_entries):
        _reject_filesystem_indirection(source, label=f"{role} model file")
        if not source.is_file():
            raise MediaError(
                MediaErrorCode.INVALID_SOURCE,
                f"{role} model file changed before runtime snapshot",
            )
        destination_dir = snapshot_root / f"{index:04d}"
        destination_dir.mkdir()
        destination = destination_dir / source.name
        _copy_regular_file(source, destination)
        role_paths.append((role, destination))
        snapshot_entries.append((role, destination))

    evidence = _evidence_for_entries(snapshot_entries, compute_sha256=True)
    return LocalModelRuntimeSnapshot(
        root=snapshot_root,
        evidence=evidence,
        _owner=owner,
        role_paths=tuple(role_paths),
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


def _collect_directory_entries(root: Path) -> list[tuple[str, Path]]:
    entries: list[tuple[str, Path]] = []
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        dirnames.sort()
        filenames.sort()

        for dirname in tuple(dirnames):
            _reject_filesystem_indirection(
                current_path / dirname,
                label="model directory",
            )
        for filename in filenames:
            child = current_path / filename
            _reject_filesystem_indirection(child, label="model file")
            if not child.is_file():
                raise MediaError(
                    MediaErrorCode.INVALID_SOURCE,
                    f"model bundle entry is not a regular file: {child.name}",
                )
            entries.append((child.relative_to(root).as_posix(), child))

    if not entries:
        raise MediaError(
            MediaErrorCode.COMPONENT_MISSING,
            "local model directory contains no files",
        )
    return entries


def _collect_role_entries(files: Mapping[str, Path]) -> list[tuple[str, Path]]:
    if not files:
        raise ValueError("model file mapping must not be empty")

    entries: list[tuple[str, Path]] = []
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
    return entries


def _reject_filesystem_indirection(path: Path, *, label: str) -> None:
    lexical = path.absolute()
    for candidate in (lexical, *lexical.parents):
        if candidate.is_symlink():
            raise MediaError(
                MediaErrorCode.PATH_ESCAPE,
                f"{label} must not traverse a symbolic link",
            )
        is_junction = getattr(candidate, "is_junction", None)
        if callable(is_junction) and is_junction():
            raise MediaError(
                MediaErrorCode.PATH_ESCAPE,
                f"{label} must not traverse a junction",
            )


def _copy_regular_file(source: Path, destination: Path) -> None:
    with source.open("rb") as reader, destination.open("xb") as writer:
        while chunk := reader.read(_READ_CHUNK_BYTES):
            writer.write(chunk)


def _evidence_for_entries(
    entries: list[tuple[str, Path]],
    *,
    compute_sha256: bool,
) -> LocalModelEvidence:
    total_size = sum(path.stat().st_size for _, path in entries)
    digest = _hash_entries(entries) if compute_sha256 else None
    return LocalModelEvidence(
        size_bytes=total_size,
        file_count=len(entries),
        sha256=digest,
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
