from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from nika_core.reliability.backup import (
    BackupArtifact,
    BackupRecoveryError,
    InterruptedRestoreResult,
    RestorePlan,
    RestoreResult,
    SQLiteRecoveryManager,
)

_RELEASE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SNAPSHOT_DATABASE = "database.sqlite3"
_CANONICAL_MANIFEST = "database.sqlite3.manifest.json"
_RELEASE_METADATA = "release-database-snapshot.json"
_RELEASE_METADATA_VERSION = 1
_RESTORE_FINGERPRINT_VERSION = b"nika-release-database-restore-v1\x00"
_RELEASE_METADATA_FIELDS = {
    "manifest_version",
    "source_release_sha",
    "database_file",
    "canonical_manifest_file",
    "database_sha256",
    "database_size",
    "database_schema_version",
    "database_created_at",
}


class ReleaseDatabaseRecoveryError(BackupRecoveryError):
    """Raised when release-specific database recovery evidence is unsafe."""


@dataclass(frozen=True, slots=True)
class ReleaseDatabaseSnapshot:
    snapshot_dir: Path
    source_release_sha: str
    database_file: str
    canonical_manifest_file: str
    database_sha256: str
    database_size: int
    database_schema_version: int
    database_created_at: str
    manifest_version: int = _RELEASE_METADATA_VERSION


@dataclass(frozen=True, slots=True)
class ReleaseDatabaseRestorePlan:
    snapshot: ReleaseDatabaseSnapshot
    canonical_plan: RestorePlan
    current_release_sha: str | None
    confirmation_fingerprint: str


@dataclass(frozen=True, slots=True)
class ReleaseDatabaseRestoreResult:
    snapshot: ReleaseDatabaseSnapshot
    canonical_result: RestoreResult


class ReleaseDatabaseRecovery:
    """Release-SHA binding over the canonical SQLiteRecoveryManager."""

    def __init__(self, recovery_manager: SQLiteRecoveryManager) -> None:
        self._recovery = recovery_manager

    def create_snapshot(
        self,
        snapshot_dir: Path | str,
        *,
        source_release_sha: str,
    ) -> ReleaseDatabaseSnapshot:
        """Publish an exact-release snapshot using canonical M10 backup semantics."""
        root = Path(snapshot_dir)
        exact_source_sha = _exact_release_sha(source_release_sha)
        if root.exists():
            return self.verify_snapshot(
                root,
                expected_source_release_sha=exact_source_sha,
            )

        root.parent.mkdir(parents=True, exist_ok=True)
        _require_plain_directory(root.parent, label="snapshot parent")
        stage = Path(
            tempfile.mkdtemp(
                prefix=f".{root.name}.release-snapshot-",
                dir=root.parent,
            )
        )
        try:
            backup = self._recovery.create_backup(stage / _SNAPSHOT_DATABASE)
            snapshot = _snapshot_from_backup(stage, exact_source_sha, backup)
            _write_release_metadata(stage / _RELEASE_METADATA, snapshot)
            _fsync_directory(stage)
            os.replace(stage, root)
            _fsync_directory(root.parent)
            return self.verify_snapshot(
                root,
                expected_source_release_sha=exact_source_sha,
            )
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    def verify_snapshot(
        self,
        snapshot_dir: Path | str,
        *,
        expected_source_release_sha: str | None = None,
    ) -> ReleaseDatabaseSnapshot:
        """Verify release metadata and delegate database verification to M10."""
        root = Path(snapshot_dir)
        _require_plain_directory(root, label="release database snapshot")
        entries = {entry.name for entry in root.iterdir()}
        expected_entries = {
            _SNAPSHOT_DATABASE,
            _CANONICAL_MANIFEST,
            _RELEASE_METADATA,
        }
        if entries != expected_entries:
            raise ReleaseDatabaseRecoveryError(
                "release database snapshot contents do not match schema"
            )

        snapshot = _read_release_metadata(root / _RELEASE_METADATA, root)
        if expected_source_release_sha is not None:
            expected_sha = _exact_release_sha(expected_source_release_sha)
            if snapshot.source_release_sha != expected_sha:
                raise ReleaseDatabaseRecoveryError(
                    "release database snapshot source SHA does not match expectation"
                )

        backup = self._recovery.verify_backup(root / _SNAPSHOT_DATABASE)
        _require_backup_matches_snapshot(backup, snapshot)
        return snapshot

    def prepare_restore(
        self,
        snapshot_dir: Path | str,
        *,
        expected_source_release_sha: str,
        current_release_sha: str | None,
    ) -> ReleaseDatabaseRestorePlan:
        """Prepare canonical restore plus exact source/current release binding."""
        snapshot = self.verify_snapshot(
            snapshot_dir,
            expected_source_release_sha=expected_source_release_sha,
        )
        canonical_plan = self._recovery.prepare_restore(
            snapshot.snapshot_dir / snapshot.database_file
        )
        normalized_current_sha: str | None = None
        if canonical_plan.current_exists:
            if current_release_sha is None:
                raise ReleaseDatabaseRecoveryError(
                    "current release SHA is required when a live database exists"
                )
            normalized_current_sha = _exact_release_sha(current_release_sha)
        elif current_release_sha is not None:
            raise ReleaseDatabaseRecoveryError(
                "current release SHA must be absent when no live database exists"
            )

        confirmation = _release_restore_fingerprint(
            snapshot,
            canonical_plan,
            normalized_current_sha,
        )
        return ReleaseDatabaseRestorePlan(
            snapshot=snapshot,
            canonical_plan=canonical_plan,
            current_release_sha=normalized_current_sha,
            confirmation_fingerprint=confirmation,
        )

    def restore(
        self,
        plan: ReleaseDatabaseRestorePlan,
        *,
        confirmation_fingerprint: str,
        allow_replace_unrecoverable_current: bool = False,
    ) -> ReleaseDatabaseRestoreResult:
        """Execute canonical restore only after release identity is reverified."""
        expected_confirmation = _release_restore_fingerprint(
            plan.snapshot,
            plan.canonical_plan,
            plan.current_release_sha,
        )
        if not _equal(confirmation_fingerprint, expected_confirmation):
            raise PermissionError(
                "release database restore confirmation does not match prepared preview"
            )
        current_snapshot = self.verify_snapshot(
            plan.snapshot.snapshot_dir,
            expected_source_release_sha=plan.snapshot.source_release_sha,
        )
        if current_snapshot != plan.snapshot:
            raise ReleaseDatabaseRecoveryError(
                "release database snapshot changed after restore preview"
            )

        result = self._recovery.restore(
            plan.canonical_plan,
            confirmation_fingerprint=plan.canonical_plan.confirmation_fingerprint,
            allow_replace_unrecoverable_current=allow_replace_unrecoverable_current,
        )
        return ReleaseDatabaseRestoreResult(
            snapshot=current_snapshot,
            canonical_result=result,
        )

    def recover_interrupted_restore(self) -> InterruptedRestoreResult | None:
        """Delegate crash recovery to the canonical M10 recovery engine."""
        return self._recovery.recover_interrupted_restore()


def _exact_release_sha(value: str) -> str:
    if not isinstance(value, str) or not _RELEASE_SHA_RE.fullmatch(value):
        raise ValueError("release recovery requires an exact lowercase 40-character SHA")
    return value


def _is_reparse_or_symlink(path: Path) -> bool:
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _require_plain_directory(path: Path, *, label: str) -> None:
    if not path.exists():
        raise ReleaseDatabaseRecoveryError(f"{label} is missing: {path}")
    if _is_reparse_or_symlink(path):
        raise ReleaseDatabaseRecoveryError(
            f"{label} may not be a symlink or reparse point: {path}"
        )
    if not path.is_dir():
        raise ReleaseDatabaseRecoveryError(f"{label} is not a directory: {path}")


def _snapshot_from_backup(
    root: Path,
    source_release_sha: str,
    backup: BackupArtifact,
) -> ReleaseDatabaseSnapshot:
    if backup.database_path.name != _SNAPSHOT_DATABASE:
        raise ReleaseDatabaseRecoveryError("canonical backup database identity is invalid")
    if backup.manifest_path.name != _CANONICAL_MANIFEST:
        raise ReleaseDatabaseRecoveryError("canonical backup manifest identity is invalid")
    return ReleaseDatabaseSnapshot(
        snapshot_dir=root,
        source_release_sha=source_release_sha,
        database_file=_SNAPSHOT_DATABASE,
        canonical_manifest_file=_CANONICAL_MANIFEST,
        database_sha256=backup.sha256,
        database_size=backup.size_bytes,
        database_schema_version=backup.schema_version,
        database_created_at=backup.created_at,
    )


def _require_backup_matches_snapshot(
    backup: BackupArtifact,
    snapshot: ReleaseDatabaseSnapshot,
) -> None:
    expected = (
        snapshot.database_file,
        snapshot.canonical_manifest_file,
        snapshot.database_sha256,
        snapshot.database_size,
        snapshot.database_schema_version,
        snapshot.database_created_at,
    )
    actual = (
        backup.database_path.name,
        backup.manifest_path.name,
        backup.sha256,
        backup.size_bytes,
        backup.schema_version,
        backup.created_at,
    )
    if actual != expected:
        raise ReleaseDatabaseRecoveryError(
            "canonical backup evidence does not match release metadata"
        )


def _write_release_metadata(path: Path, snapshot: ReleaseDatabaseSnapshot) -> None:
    payload = asdict(snapshot)
    payload.pop("snapshot_dir")
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_release_metadata(path: Path, root: Path) -> ReleaseDatabaseSnapshot:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseDatabaseRecoveryError(
            "release database metadata is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(payload, dict) or set(payload) != _RELEASE_METADATA_FIELDS:
        raise ReleaseDatabaseRecoveryError(
            "release database metadata fields do not match schema"
        )

    manifest_version = _exact_int(payload, "manifest_version")
    if manifest_version != _RELEASE_METADATA_VERSION:
        raise ReleaseDatabaseRecoveryError(
            f"unsupported release database metadata version: {manifest_version}"
        )
    source_release_sha = _exact_release_sha(_text(payload, "source_release_sha"))
    database_file = _text(payload, "database_file")
    canonical_manifest_file = _text(payload, "canonical_manifest_file")
    if database_file != _SNAPSHOT_DATABASE:
        raise ReleaseDatabaseRecoveryError("release snapshot database identity is invalid")
    if canonical_manifest_file != _CANONICAL_MANIFEST:
        raise ReleaseDatabaseRecoveryError(
            "release snapshot canonical manifest identity is invalid"
        )

    database_sha256 = _text(payload, "database_sha256")
    if not re.fullmatch(r"[0-9a-f]{64}", database_sha256):
        raise ReleaseDatabaseRecoveryError("release snapshot SHA-256 is invalid")
    created_at = _text(payload, "database_created_at")
    return ReleaseDatabaseSnapshot(
        snapshot_dir=root,
        source_release_sha=source_release_sha,
        database_file=database_file,
        canonical_manifest_file=canonical_manifest_file,
        database_sha256=database_sha256,
        database_size=_exact_int(payload, "database_size"),
        database_schema_version=_exact_int(payload, "database_schema_version"),
        database_created_at=created_at,
        manifest_version=manifest_version,
    )


def _exact_int(payload: dict[str, Any], field: str) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReleaseDatabaseRecoveryError(
            f"release database metadata has invalid {field}"
        )
    return value


def _text(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise ReleaseDatabaseRecoveryError(
            f"release database metadata has invalid {field}"
        )
    return value


def _release_restore_fingerprint(
    snapshot: ReleaseDatabaseSnapshot,
    canonical_plan: RestorePlan,
    current_release_sha: str | None,
) -> str:
    digest = hashlib.sha256()
    digest.update(_RESTORE_FINGERPRINT_VERSION)
    payload = json.dumps(
        {
            "source_release_sha": snapshot.source_release_sha,
            "snapshot_sha256": snapshot.database_sha256,
            "current_release_sha": current_release_sha,
            "canonical_confirmation_fingerprint": (
                canonical_plan.confirmation_fingerprint
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest.update(payload)
    return digest.hexdigest()


def _equal(left: str, right: str) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return hmac.compare_digest(left.encode(), right.encode())


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
