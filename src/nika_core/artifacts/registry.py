from __future__ import annotations

import hashlib
import mimetypes
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from nika_core.artifacts.contracts import (
    ArtifactLocationKind,
    ArtifactRecord,
    ArtifactRegistryError,
    ArtifactVerification,
    ArtifactVerificationState,
)
from nika_core.artifacts.repository import SQLiteArtifactRepository
from nika_core.artifacts.schema import initialize_artifact_registry_schema
from nika_core.data.sqlite import SQLiteStore

Clock = Callable[[], datetime]


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _artifact_id(workspace_id: str, idempotency_key: str) -> str:
    return _sha256_text(f"{workspace_id}\0{idempotency_key}")


def _verification_id(
    *,
    artifact_id: str,
    checked_at: datetime,
    state: ArtifactVerificationState,
    actual_sha256: str | None,
    actual_size_bytes: int | None,
) -> str:
    material = "\0".join(
        (
            artifact_id,
            checked_at.isoformat(),
            state.value,
            actual_sha256 or "",
            "" if actual_size_bytes is None else str(actual_size_bytes),
        )
    )
    return _sha256_text(material)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _read_clock(clock: Clock) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ArtifactRegistryError("artifact registry clock must be timezone-aware")
    return value.astimezone(UTC)


def _hash_open_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            total = 0
            while chunk := handle.read(1024 * 1024):
                total += len(chunk)
                digest.update(chunk)
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise ArtifactRegistryError("artifact file could not be read") from exc

    before_mtime_ns = getattr(before, "st_mtime_ns", None)
    after_mtime_ns = getattr(after, "st_mtime_ns", None)
    if before.st_size != after.st_size or before_mtime_ns != after_mtime_ns:
        raise ArtifactRegistryError("artifact file changed while it was being hashed")
    if total != after.st_size:
        raise ArtifactRegistryError("artifact file size changed while it was being hashed")
    return digest.hexdigest(), total


class ArtifactRegistry:
    """Durable artifact metadata registry; artifact bytes remain owned by their storage layer."""

    def __init__(
        self,
        repository: SQLiteArtifactRepository,
        *,
        clock: Clock = _utc_now,
        local_file_roots: tuple[Path | str, ...] = (),
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._local_file_roots = self._resolve_local_file_roots(local_file_roots)

    @classmethod
    def from_store(
        cls,
        store: SQLiteStore,
        *,
        clock: Clock = _utc_now,
        local_file_roots: tuple[Path | str, ...] = (),
    ) -> ArtifactRegistry:
        initialize_artifact_registry_schema(store)
        return cls(
            SQLiteArtifactRepository(store),
            clock=clock,
            local_file_roots=local_file_roots,
        )

    def register_file(
        self,
        *,
        workspace_id: str,
        idempotency_key: str,
        path: Path | str,
        kind: str,
        display_name: str = "",
        media_type: str | None = None,
        producer_type: str | None = None,
        producer_id: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ArtifactRecord:
        source = Path(path)
        try:
            resolved = source.resolve(strict=True)
        except OSError as exc:
            raise ArtifactRegistryError("artifact source does not exist") from exc
        if not resolved.is_file():
            raise ArtifactRegistryError("artifact source is not a regular file")
        if not self._local_file_roots:
            raise ArtifactRegistryError(
                "local file registration is disabled until an allowed root is configured"
            )
        if not any(resolved.is_relative_to(root) for root in self._local_file_roots):
            raise ArtifactRegistryError("artifact source escapes configured local file roots")

        sha256, size_bytes = _hash_open_file(resolved)
        inferred_media_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
        record = self._build_record(
            workspace_id=workspace_id,
            idempotency_key=idempotency_key,
            kind=kind,
            display_name=display_name or resolved.name,
            location_kind=ArtifactLocationKind.LOCAL_FILE,
            locator=str(resolved),
            sha256=sha256,
            size_bytes=size_bytes,
            media_type=media_type or inferred_media_type,
            producer_type=producer_type,
            producer_id=producer_id,
            metadata=metadata or {},
        )
        return self._repository.put_record(record)

    def register_reference(
        self,
        *,
        workspace_id: str,
        idempotency_key: str,
        reference: str,
        sha256: str,
        size_bytes: int,
        kind: str,
        display_name: str = "",
        media_type: str = "application/octet-stream",
        producer_type: str | None = None,
        producer_id: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ArtifactRecord:
        record = self._build_record(
            workspace_id=workspace_id,
            idempotency_key=idempotency_key,
            kind=kind,
            display_name=display_name,
            location_kind=ArtifactLocationKind.OPAQUE_REFERENCE,
            locator=reference,
            sha256=sha256,
            size_bytes=size_bytes,
            media_type=media_type,
            producer_type=producer_type,
            producer_id=producer_id,
            metadata=metadata or {},
        )
        return self._repository.put_record(record)

    def get(self, artifact_id: str) -> ArtifactRecord:
        return self._repository.get(artifact_id)

    def list(
        self,
        *,
        workspace_id: str | None = None,
        kind: str | None = None,
        producer_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[ArtifactRecord, ...]:
        return self._repository.list_records(
            workspace_id=workspace_id,
            kind=kind,
            producer_id=producer_id,
            limit=limit,
            offset=offset,
        )

    def find_by_sha256(
        self,
        sha256: str,
        *,
        workspace_id: str | None = None,
    ) -> tuple[ArtifactRecord, ...]:
        return self._repository.find_by_sha256(sha256, workspace_id=workspace_id)

    def verify(self, artifact_id: str) -> ArtifactVerification:
        record = self._repository.get(artifact_id)
        checked_at = _read_clock(self._clock)

        if record.location_kind == ArtifactLocationKind.OPAQUE_REFERENCE:
            verification = self._build_verification(
                record=record,
                checked_at=checked_at,
                state=ArtifactVerificationState.UNAVAILABLE,
                detail=(
                    "opaque artifact references require their owning storage adapter "
                    "to verify bytes"
                ),
            )
            return self._repository.put_verification(verification)

        path = Path(record.locator)
        if not path.is_file():
            verification = self._build_verification(
                record=record,
                checked_at=checked_at,
                state=ArtifactVerificationState.MISSING,
                detail="registered local artifact is missing",
            )
            return self._repository.put_verification(verification)

        try:
            actual_sha256, actual_size = _hash_open_file(path)
        except ArtifactRegistryError:
            verification = self._build_verification(
                record=record,
                checked_at=checked_at,
                state=ArtifactVerificationState.MISMATCH,
                detail="registered local artifact could not be verified consistently",
            )
            return self._repository.put_verification(verification)

        state = (
            ArtifactVerificationState.VERIFIED
            if actual_sha256 == record.sha256 and actual_size == record.size_bytes
            else ArtifactVerificationState.MISMATCH
        )
        detail = (
            "registered local artifact matches immutable metadata"
            if state == ArtifactVerificationState.VERIFIED
            else "registered local artifact bytes differ from immutable metadata"
        )
        verification = self._build_verification(
            record=record,
            checked_at=checked_at,
            state=state,
            actual_sha256=actual_sha256,
            actual_size_bytes=actual_size,
            detail=detail,
        )
        return self._repository.put_verification(verification)

    def verification_history(self, artifact_id: str) -> tuple[ArtifactVerification, ...]:
        self._repository.get(artifact_id)
        return self._repository.list_verifications(artifact_id)

    @staticmethod
    def _resolve_local_file_roots(
        roots: tuple[Path | str, ...],
    ) -> tuple[Path, ...]:
        resolved_roots: list[Path] = []
        for root in roots:
            try:
                resolved = Path(root).resolve(strict=True)
            except OSError as exc:
                raise ArtifactRegistryError("configured local file root does not exist") from exc
            if not resolved.is_dir():
                raise ArtifactRegistryError("configured local file root is not a directory")
            if resolved not in resolved_roots:
                resolved_roots.append(resolved)
        return tuple(resolved_roots)

    def _build_record(
        self,
        *,
        workspace_id: str,
        idempotency_key: str,
        kind: str,
        display_name: str,
        location_kind: ArtifactLocationKind,
        locator: str,
        sha256: str,
        size_bytes: int,
        media_type: str,
        producer_type: str | None,
        producer_id: str | None,
        metadata: dict[str, str],
    ) -> ArtifactRecord:
        return ArtifactRecord(
            artifact_id=_artifact_id(workspace_id, idempotency_key),
            idempotency_key=idempotency_key,
            workspace_id=workspace_id,
            kind=kind,
            display_name=display_name,
            location_kind=location_kind,
            locator=locator,
            sha256=sha256,
            size_bytes=size_bytes,
            media_type=media_type,
            producer_type=producer_type,
            producer_id=producer_id,
            metadata=metadata,
            created_at=_read_clock(self._clock),
        )

    @staticmethod
    def _build_verification(
        *,
        record: ArtifactRecord,
        checked_at: datetime,
        state: ArtifactVerificationState,
        actual_sha256: str | None = None,
        actual_size_bytes: int | None = None,
        detail: str = "",
    ) -> ArtifactVerification:
        return ArtifactVerification(
            verification_id=_verification_id(
                artifact_id=record.artifact_id,
                checked_at=checked_at,
                state=state,
                actual_sha256=actual_sha256,
                actual_size_bytes=actual_size_bytes,
            ),
            artifact_id=record.artifact_id,
            state=state,
            expected_sha256=record.sha256,
            actual_sha256=actual_sha256,
            expected_size_bytes=record.size_bytes,
            actual_size_bytes=actual_size_bytes,
            checked_at=checked_at,
            detail=detail,
        )
