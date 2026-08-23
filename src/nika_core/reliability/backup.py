from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from nika_core.data.schema import SCHEMA_VERSION
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.reliability.recovery_lease import (
    RecoveryFileLease,
    RecoveryLeaseError,
    exclusive_sqlite_lease,
)

_MANIFEST_VERSION = 1
_RESTORE_MARKER_VERSION = 1
_RESTORE_STATE_VERSION = b"nika-sqlite-restore-state-v2\x00"


class BackupRecoveryError(RuntimeError):
    """Base error for backup/restore safety failures."""


class BackupVerificationError(BackupRecoveryError):
    """Backup or staged database cannot be trusted."""


class RestorePlanStaleError(BackupRecoveryError):
    """Live state changed after restore preview."""


class RestoreSafetyError(BackupRecoveryError):
    """Restore cannot proceed without violating recovery guarantees."""


class InterruptedRestoreDisposition(StrEnum):
    COMPLETED = "completed"
    ROLLED_BACK = "rolled_back"


@dataclass(frozen=True, slots=True)
class BackupArtifact:
    database_path: Path
    manifest_path: Path
    sha256: str
    size_bytes: int
    schema_version: int
    created_at: str


@dataclass(frozen=True, slots=True)
class RestorePlan:
    backup: BackupArtifact
    target_path: Path
    current_exists: bool
    current_sha256: str | None
    current_schema_version: int | None
    current_is_healthy: bool
    confirmation_fingerprint: str


@dataclass(frozen=True, slots=True)
class RestoreResult:
    target_path: Path
    restored_schema_version: int
    safety_backup: BackupArtifact | None
    quarantined_database: Path | None
    quarantine_manifest: Path | None


@dataclass(frozen=True, slots=True)
class InterruptedRestoreResult:
    disposition: InterruptedRestoreDisposition
    target_path: Path
    quarantined_database: Path | None


class SQLiteRecoveryManager:
    """Crash-aware backup/restore policy around Nika's authoritative SQLite store."""

    def __init__(self, store: SQLiteStore, audit: AuditLog | None = None) -> None:
        self._store = store
        self._audit = audit or AuditLog(store)

    def create_backup(self, backup_path: Path | str) -> BackupArtifact:
        with self._hold_recovery_lease():
            return self._create_backup(backup_path, record_audit=True)

    def _create_backup(
        self,
        backup_path: Path | str,
        *,
        record_audit: bool,
        source_connection: sqlite3.Connection | None = None,
    ) -> BackupArtifact:
        self._ensure_no_interrupted_restore()
        source = self._store.path.resolve()
        destination = Path(backup_path).resolve()
        manifest_path = self._manifest_path(destination)
        if not source.is_file():
            raise FileNotFoundError(f"database does not exist: {source}")
        if destination == source:
            raise ValueError("backup destination must differ from the live database")
        if destination.exists() or manifest_path.exists():
            raise FileExistsError("backup database or manifest already exists")

        destination.parent.mkdir(parents=True, exist_ok=True)
        temp_db = self._temporary_path(destination, "backup")
        temp_manifest = self._temporary_path(manifest_path, "manifest")
        published_db = False
        try:
            if source_connection is None:
                self._copy_database(source, temp_db)
            else:
                self._copy_connection_to_database(source_connection, temp_db)
            schema = self._validate_database(temp_db, require_supported=True)
            digest = self._sha256_file(temp_db)
            size = temp_db.stat().st_size
            created_at = datetime.now(UTC).isoformat()
            payload = {
                "format_version": _MANIFEST_VERSION,
                "database_file": destination.name,
                "sha256": digest,
                "size_bytes": size,
                "schema_version": schema,
                "created_at": created_at,
            }
            self._write_json_fsync(temp_manifest, payload)
            self._fsync_file(temp_db)
            os.replace(temp_db, destination)
            published_db = True
            os.replace(temp_manifest, manifest_path)
            self._fsync_directory(destination.parent)
        except Exception:
            temp_db.unlink(missing_ok=True)
            temp_manifest.unlink(missing_ok=True)
            if published_db and not manifest_path.exists():
                destination.unlink(missing_ok=True)
            raise

        artifact = BackupArtifact(
            database_path=destination,
            manifest_path=manifest_path,
            sha256=digest,
            size_bytes=size,
            schema_version=schema,
            created_at=created_at,
        )
        if record_audit:
            self._audit_if_possible(
                "reliability.backup_created",
                {
                    "backup_file": destination.name,
                    "sha256": digest,
                    "size_bytes": size,
                    "schema_version": schema,
                },
            )
        return artifact

    def verify_backup(self, backup_path: Path | str) -> BackupArtifact:
        database = Path(backup_path).resolve()
        manifest_path = self._manifest_path(database)
        if not database.is_file():
            raise BackupVerificationError(f"backup database does not exist: {database}")
        if not manifest_path.is_file():
            raise BackupVerificationError(
                f"backup manifest does not exist: {manifest_path}"
            )
        if self._is_indirect_path(database) or self._is_indirect_path(manifest_path):
            raise BackupVerificationError("backup database and manifest must be direct files")
        self._ensure_backup_artifact_coherent(database)
        manifest = self._read_json(manifest_path)
        expected = {
            "format_version",
            "database_file",
            "sha256",
            "size_bytes",
            "schema_version",
            "created_at",
        }
        if set(manifest) != expected:
            raise BackupVerificationError("backup manifest has unexpected or missing fields")
        if manifest["format_version"] != _MANIFEST_VERSION:
            raise BackupVerificationError("unsupported backup manifest format")
        if manifest["database_file"] != database.name:
            raise BackupVerificationError("backup manifest does not match database filename")
        try:
            size = int(manifest["size_bytes"])
            schema = int(manifest["schema_version"])
        except (TypeError, ValueError) as exc:
            raise BackupVerificationError("backup manifest numeric fields are invalid") from exc
        if size <= 0 or database.stat().st_size != size:
            raise BackupVerificationError("backup database size does not match manifest")

        expected_sha = manifest["sha256"]
        if not isinstance(expected_sha, str) or not self._is_sha256(expected_sha):
            raise BackupVerificationError("backup manifest SHA-256 is invalid")
        actual_sha = self._sha256_file(database)
        if not self._equal(actual_sha, expected_sha):
            raise BackupVerificationError("backup database SHA-256 does not match manifest")
        if self._validate_database(database, require_supported=True) != schema:
            raise BackupVerificationError("backup schema version does not match manifest")
        self._ensure_backup_artifact_coherent(database)
        if database.stat().st_size != size:
            raise BackupVerificationError("backup database size changed during verification")
        actual_sha = self._sha256_file(database)
        if not self._equal(actual_sha, expected_sha):
            raise BackupVerificationError("backup database changed during verification")

        created_at = manifest["created_at"]
        if not isinstance(created_at, str):
            raise BackupVerificationError("backup manifest created_at is invalid")
        try:
            parsed = datetime.fromisoformat(created_at)
        except ValueError as exc:
            raise BackupVerificationError("backup manifest created_at is invalid") from exc
        if parsed.tzinfo is None:
            raise BackupVerificationError(
                "backup manifest created_at must be timezone-aware"
            )
        return BackupArtifact(database, manifest_path, actual_sha, size, schema, created_at)

    def prepare_restore(self, backup_path: Path | str) -> RestorePlan:
        with self._hold_recovery_lease():
            return self._prepare_restore_unlocked(backup_path)

    def _prepare_restore_unlocked(self, backup_path: Path | str) -> RestorePlan:
        self._ensure_no_interrupted_restore()
        backup = self.verify_backup(backup_path)
        target = self._store.path.resolve()
        self._ensure_restore_family_coherent(target)
        if target == backup.database_path:
            raise ValueError("restore source must differ from the live database")
        current_exists = target.exists()
        current_schema: int | None = None
        current_is_healthy = False
        if current_exists:
            try:
                current_schema = self._validate_database(
                    target, require_supported=False
                )
            except BackupVerificationError:
                pass
            else:
                current_is_healthy = current_schema <= SCHEMA_VERSION
        self._audit_if_possible(
            "reliability.restore_previewed",
            {
                "backup_file": backup.database_path.name,
                "backup_sha256": backup.sha256,
                "backup_schema_version": backup.schema_version,
                "current_exists": current_exists,
                "current_is_healthy": current_is_healthy,
                "current_schema_version": current_schema,
            },
        )
        self._ensure_restore_family_coherent(target)
        current_exists = target.exists()
        current_sha = self._sha256_file(target) if current_exists else None
        current_state_sha = (
            self._restore_state_sha256(target) if current_exists else None
        )
        fingerprint = self._restore_fingerprint(
            backup.sha256,
            target,
            current_exists,
            current_sha,
            current_state_sha,
        )
        return RestorePlan(
            backup,
            target,
            current_exists,
            current_sha,
            current_schema,
            current_is_healthy,
            fingerprint,
        )

    def restore(
        self,
        plan: RestorePlan,
        *,
        confirmation_fingerprint: str,
        allow_replace_unrecoverable_current: bool = False,
    ) -> RestoreResult:
        with self._hold_recovery_lease():
            return self._restore_unlocked(
                plan,
                confirmation_fingerprint=confirmation_fingerprint,
                allow_replace_unrecoverable_current=allow_replace_unrecoverable_current,
            )

    def _restore_unlocked(
        self,
        plan: RestorePlan,
        *,
        confirmation_fingerprint: str,
        allow_replace_unrecoverable_current: bool,
    ) -> RestoreResult:
        self._ensure_no_interrupted_restore()
        if not self._equal(confirmation_fingerprint, plan.confirmation_fingerprint):
            raise PermissionError("restore confirmation does not match the prepared preview")

        backup = self.verify_backup(plan.backup.database_path)
        target = self._store.path.resolve()
        if target != plan.target_path:
            raise RestorePlanStaleError("restore target changed after preview")
        current_exists = self._assert_restore_plan_current(plan, backup, target)

        target.parent.mkdir(parents=True, exist_ok=True)
        staged = self._temporary_path(target, "restore-stage")
        safety: BackupArtifact | None = None
        quarantine: Path | None = None
        quarantine_manifest: Path | None = None
        copy_completed = False
        try:
            self._stage_verified_backup(backup, staged)
            SQLiteStore(staged).initialize()
            if self._validate_database(staged, require_supported=True) != SCHEMA_VERSION:
                raise BackupVerificationError(
                    "staged restore did not migrate to the current schema"
                )

            if current_exists and plan.current_is_healthy:
                safety = self._restore_healthy_current(
                    staged=staged,
                    target=target,
                    plan=plan,
                    backup=backup,
                )
                copy_completed = True
            else:
                if current_exists and not allow_replace_unrecoverable_current:
                    raise RestoreSafetyError(
                        "current database is corrupt or unsupported; destructive replacement "
                        "requires allow_replace_unrecoverable_current=True after explicit review"
                    )
                self._append_restore_completed(
                    staged,
                    target,
                    backup,
                    None,
                    current_exists,
                )
                if current_exists:
                    quarantine, quarantine_manifest = self._replace_unrecoverable(
                        staged, target, plan, backup
                    )
                else:
                    self._publish_database_no_clobber(staged, target)
                copy_completed = True
        except Exception:
            self._audit_if_possible(
                "reliability.restore_failed",
                {
                    "backup_file": backup.database_path.name,
                    "backup_sha256": backup.sha256,
                    "copy_completed": copy_completed,
                },
            )
            raise
        finally:
            if not self._restore_marker_path(target).exists():
                staged.unlink(missing_ok=True)

        return RestoreResult(
            target, SCHEMA_VERSION, safety, quarantine, quarantine_manifest
        )

    def _restore_healthy_current(
        self,
        *,
        staged: Path,
        target: Path,
        plan: RestorePlan,
        backup: BackupArtifact,
    ) -> BackupArtifact:
        try:
            with exclusive_sqlite_lease(target) as live_connection:
                self._assert_restore_plan_current(plan, backup, target)
                safety = self._create_backup(
                    target.parent / self._safety_backup_name(target),
                    record_audit=False,
                    source_connection=live_connection,
                )
                self._append_restore_completed(staged, target, backup, safety, False)
                copy_completed = False
                try:
                    self._copy_database_to_connection(staged, live_connection)
                    copy_completed = True
                    if (
                        self._validate_connection(
                            live_connection,
                            require_supported=True,
                        )
                        != SCHEMA_VERSION
                    ):
                        raise BackupVerificationError(
                            "restored database is not on the current schema"
                        )
                except Exception:
                    if copy_completed:
                        try:
                            self._copy_database_to_connection(
                                safety.database_path,
                                live_connection,
                            )
                            self._validate_connection(
                                live_connection,
                                require_supported=False,
                            )
                        except Exception as rollback_exc:
                            raise RestoreSafetyError(
                                "restore failed and rollback from the safety backup also failed"
                            ) from rollback_exc
                    raise
                return safety
        except RecoveryLeaseError as exc:
            raise RestoreSafetyError(str(exc)) from exc

    def recover_interrupted_restore(self) -> InterruptedRestoreResult | None:
        with self._hold_recovery_lease():
            return self._recover_interrupted_restore_unlocked()

    def _recover_interrupted_restore_unlocked(self) -> InterruptedRestoreResult | None:
        target = self._store.path.resolve()
        marker_path = self._restore_marker_path(target)
        if not marker_path.exists():
            return None
        marker = self._read_restore_marker(marker_path, target)
        stage = target.parent / marker["stage_file"]
        quarantine = target.parent / marker["quarantine_file"]
        old_sha = marker["current_sha256"]
        stage_sha = marker["stage_sha256"]

        if target.exists():
            target_sha = self._sha256_file(target)
            if self._equal(target_sha, stage_sha):
                self._validate_marker_target(target, stage_sha)
                self._publish_quarantine_manifest(marker, target)
                stage.unlink(missing_ok=True)
                marker_path.unlink(missing_ok=True)
                self._fsync_directory(target.parent)
                return InterruptedRestoreResult(
                    InterruptedRestoreDisposition.COMPLETED,
                    target,
                    quarantine if quarantine.exists() else None,
                )
            if self._equal(target_sha, old_sha):
                if not stage.exists():
                    marker_path.unlink(missing_ok=True)
                    self._fsync_directory(target.parent)
                    return InterruptedRestoreResult(
                        InterruptedRestoreDisposition.ROLLED_BACK, target, None
                    )
                if quarantine.exists():
                    raise RestoreSafetyError(
                        "interrupted restore has both live pre-restore bytes "
                        "and an existing quarantine"
                    )
                os.replace(target, quarantine)
                self._move_sidecars_to_quarantine(target, marker)
                self._fsync_directory(target.parent)
            elif quarantine.exists():
                self._rollback_quarantine(target, marker)
                marker_path.unlink(missing_ok=True)
                stage.unlink(missing_ok=True)
                self._fsync_directory(target.parent)
                return InterruptedRestoreResult(
                    InterruptedRestoreDisposition.ROLLED_BACK, target, None
                )
            else:
                raise RestoreSafetyError(
                    "interrupted restore target is unknown and no quarantine is available"
                )

        if not target.exists() and stage.exists():
            self._move_sidecars_to_quarantine(target, marker)
            self._publish_database_no_clobber(stage, target)
            self._validate_marker_target(target, stage_sha)
            self._publish_quarantine_manifest(marker, target)
            stage.unlink(missing_ok=True)
            marker_path.unlink(missing_ok=True)
            self._fsync_directory(target.parent)
            return InterruptedRestoreResult(
                InterruptedRestoreDisposition.COMPLETED,
                target,
                quarantine if quarantine.exists() else None,
            )
        if not target.exists() and quarantine.exists():
            self._rollback_quarantine(target, marker)
            marker_path.unlink(missing_ok=True)
            self._fsync_directory(target.parent)
            return InterruptedRestoreResult(
                InterruptedRestoreDisposition.ROLLED_BACK, target, None
            )
        raise RestoreSafetyError(
            "interrupted restore has no recoverable staged or quarantined database"
        )

    def _replace_unrecoverable(
        self,
        staged: Path,
        target: Path,
        plan: RestorePlan,
        backup: BackupArtifact,
    ) -> tuple[Path, Path]:
        marker_path = self._restore_marker_path(target)
        if marker_path.exists():
            raise RestoreSafetyError("an interrupted restore marker already exists")
        if plan.current_sha256 is None or not target.exists():
            raise RestorePlanStaleError(
                "unrecoverable target disappeared or has no preview hash"
            )
        quarantine = target.parent / self._quarantine_name(target)
        quarantine_manifest = self._manifest_path(quarantine)
        wal_quarantine = target.parent / f"{quarantine.name}-wal"
        shm_quarantine = target.parent / f"{quarantine.name}-shm"
        if any(
            path.exists()
            for path in (quarantine, quarantine_manifest, wal_quarantine, shm_quarantine)
        ):
            raise RestoreSafetyError("quarantine destination already exists")

        stage_sha = self._sha256_file(staged)
        marker: dict[str, object] = {
            "format_version": _RESTORE_MARKER_VERSION,
            "target_file": target.name,
            "stage_file": staged.name,
            "stage_sha256": stage_sha,
            "quarantine_file": quarantine.name,
            "quarantine_wal_file": wal_quarantine.name,
            "quarantine_shm_file": shm_quarantine.name,
            "current_sha256": plan.current_sha256,
            "backup_sha256": backup.sha256,
            "created_at": datetime.now(UTC).isoformat(),
        }
        self._publish_marker(marker_path, marker)
        typed_marker = self._read_restore_marker(marker_path, target)
        try:
            os.replace(target, quarantine)
            self._move_sidecars_to_quarantine(target, typed_marker)
            self._fsync_directory(target.parent)
            self._publish_database_no_clobber(staged, target)
            self._validate_marker_target(target, stage_sha)
            quarantine_manifest = self._publish_quarantine_manifest(
                typed_marker, target
            )
            marker_path.unlink(missing_ok=True)
            self._fsync_directory(target.parent)
        except Exception:
            try:
                self._rollback_quarantine(target, typed_marker)
            except Exception as rollback_exc:
                raise RestoreSafetyError(
                    "unrecoverable replacement and quarantine rollback both failed; "
                    "restore marker retained"
                ) from rollback_exc
            marker_path.unlink(missing_ok=True)
            self._fsync_directory(target.parent)
            raise
        return quarantine, quarantine_manifest

    def _rollback_quarantine(self, target: Path, marker: dict[str, Any]) -> None:
        quarantine = target.parent / marker["quarantine_file"]
        old_sha = marker["current_sha256"]
        stage_sha = marker["stage_sha256"]
        failed: Path | None = None
        if target.exists():
            target_sha = self._sha256_file(target)
            if self._equal(target_sha, old_sha):
                return
            if not self._equal(target_sha, stage_sha):
                raise RestoreSafetyError(
                    "quarantine rollback refused to overwrite an unknown restore target"
                )
            failed = self._temporary_path(target, "failed-restore")
            os.replace(target, failed)
        if not quarantine.exists():
            if failed is not None and failed.exists():
                os.replace(failed, target)
            raise RestoreSafetyError("quarantined pre-restore database is missing")
        os.replace(quarantine, target)
        self._restore_sidecars(target, marker)
        if failed is not None:
            failed.unlink(missing_ok=True)
        if not self._equal(self._sha256_file(target), old_sha):
            raise RestoreSafetyError(
                "quarantine rollback did not reproduce the pre-restore bytes"
            )
        self._manifest_path(quarantine).unlink(missing_ok=True)
        self._fsync_directory(target.parent)

    def _move_sidecars_to_quarantine(
        self, target: Path, marker: dict[str, Any]
    ) -> None:
        pairs = (
            (self._wal_path(target), target.parent / marker["quarantine_wal_file"]),
            (self._shm_path(target), target.parent / marker["quarantine_shm_file"]),
        )
        for live, quarantined in pairs:
            if live.exists():
                if quarantined.exists():
                    raise RestoreSafetyError("quarantine sidecar destination already exists")
                os.replace(live, quarantined)

    def _restore_sidecars(self, target: Path, marker: dict[str, Any]) -> None:
        pairs = (
            (target.parent / marker["quarantine_wal_file"], self._wal_path(target)),
            (target.parent / marker["quarantine_shm_file"], self._shm_path(target)),
        )
        for quarantined, live in pairs:
            if quarantined.exists():
                live.unlink(missing_ok=True)
                os.replace(quarantined, live)

    def _publish_quarantine_manifest(
        self, marker: dict[str, Any], target: Path
    ) -> Path:
        quarantine = target.parent / marker["quarantine_file"]
        path = self._manifest_path(quarantine)
        if path.exists():
            return path
        sidecars = [
            name
            for name in (
                marker["quarantine_wal_file"],
                marker["quarantine_shm_file"],
            )
            if (target.parent / name).exists()
        ]
        payload = {
            "format_version": 1,
            "target_file": target.name,
            "quarantine_file": quarantine.name,
            "quarantine_sidecars": sidecars,
            "pre_restore_sha256": marker["current_sha256"],
            "backup_sha256": marker["backup_sha256"],
            "restored_sha256": marker["stage_sha256"],
            "created_at": marker["created_at"],
            "trusted_database": False,
            "reason": "pre-restore database failed Nika integrity/support validation",
        }
        temporary = self._temporary_path(path, "quarantine-manifest")
        try:
            self._write_json_fsync(temporary, payload)
            os.replace(temporary, path)
            self._fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def _publish_marker(self, marker_path: Path, marker: dict[str, object]) -> None:
        temporary = self._temporary_path(marker_path, "restore-marker")
        try:
            self._write_json_fsync(temporary, marker)
            os.replace(temporary, marker_path)
            self._fsync_directory(marker_path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def _read_restore_marker(
        self, marker_path: Path, target: Path
    ) -> dict[str, Any]:
        marker = self._read_json(marker_path)
        expected = {
            "format_version",
            "target_file",
            "stage_file",
            "stage_sha256",
            "quarantine_file",
            "quarantine_wal_file",
            "quarantine_shm_file",
            "current_sha256",
            "backup_sha256",
            "created_at",
        }
        if set(marker) != expected:
            raise RestoreSafetyError(
                "interrupted restore marker has unexpected or missing fields"
            )
        if marker["format_version"] != _RESTORE_MARKER_VERSION:
            raise RestoreSafetyError("unsupported interrupted restore marker format")
        if marker["target_file"] != target.name:
            raise RestoreSafetyError("interrupted restore marker targets another database")
        for key in (
            "stage_file",
            "quarantine_file",
            "quarantine_wal_file",
            "quarantine_shm_file",
        ):
            value = marker[key]
            if not isinstance(value, str) or Path(value).name != value:
                raise RestoreSafetyError(
                    "interrupted restore marker contains an unsafe path"
                )
        for key in ("stage_sha256", "current_sha256", "backup_sha256"):
            value = marker[key]
            if not isinstance(value, str) or not self._is_sha256(value):
                raise RestoreSafetyError(
                    "interrupted restore marker contains an invalid hash"
                )
        return marker

    def _append_restore_completed(
        self,
        staged: Path,
        target: Path,
        backup: BackupArtifact,
        safety: BackupArtifact | None,
        replaced_unrecoverable: bool,
    ) -> None:
        AuditLog(SQLiteStore(staged)).append(
            event_type="reliability.restore_completed",
            entity_type="database",
            entity_id=str(target),
            payload={
                "backup_file": backup.database_path.name,
                "backup_sha256": backup.sha256,
                "restored_schema_version": SCHEMA_VERSION,
                "safety_backup_file": (
                    safety.database_path.name if safety is not None else None
                ),
                "replaced_unrecoverable_current": replaced_unrecoverable,
            },
        )

    def _validate_marker_target(self, target: Path, expected_sha: str) -> None:
        if not self._equal(self._sha256_file(target), expected_sha):
            raise RestoreSafetyError(
                "restored database bytes differ from the durable restore marker"
            )
        if self._validate_database(target, require_supported=True) != SCHEMA_VERSION:
            raise RestoreSafetyError("restored database did not reach current schema")

    def _audit_if_possible(
        self, event_type: str, payload: dict[str, object]
    ) -> None:
        target = self._store.path.resolve()
        if not self._audit_target_is_safe(target):
            return
        try:
            self._audit.append(
                event_type=event_type,
                entity_type="database",
                entity_id=str(target),
                payload=payload,
            )
        except (sqlite3.DatabaseError, OSError):
            return

    @staticmethod
    def _audit_target_is_safe(target: Path) -> bool:
        if not target.is_file():
            return False
        try:
            conn = sqlite3.connect(
                f"{target.as_uri()}?mode=ro",
                uri=True,
                timeout=1.0,
            )
            try:
                integrity = tuple(
                    str(row[0]) for row in conn.execute("PRAGMA integrity_check")
                )
                if integrity != ("ok",):
                    return False
                if conn.execute("PRAGMA foreign_key_check").fetchall():
                    return False
                versions = tuple(
                    int(row[0])
                    for row in conn.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                )
            finally:
                conn.close()
        except (sqlite3.DatabaseError, OSError, ValueError):
            return False
        return bool(versions) and versions == tuple(
            range(1, versions[-1] + 1)
        ) and versions[-1] <= SCHEMA_VERSION

    def _ensure_no_interrupted_restore(self) -> None:
        if self._restore_marker_path(self._store.path.resolve()).exists():
            raise RestoreSafetyError(
                "an interrupted restore marker exists; call "
                "recover_interrupted_restore() before new backup/restore work"
            )

    @contextmanager
    def _hold_recovery_lease(self) -> Iterator[None]:
        target = self._store.path.resolve()
        try:
            with RecoveryFileLease(self._recovery_lease_path(target)):
                yield
        except RecoveryLeaseError as exc:
            raise RestoreSafetyError(str(exc)) from exc

    def _assert_restore_plan_current(
        self,
        plan: RestorePlan,
        backup: BackupArtifact,
        target: Path,
    ) -> bool:
        try:
            self._ensure_restore_family_coherent(target)
            current_exists = target.exists()
            current_sha = self._sha256_file(target) if current_exists else None
            current_state_sha = (
                self._restore_state_sha256(target) if current_exists else None
            )
        except (OSError, BackupRecoveryError) as exc:
            raise RestorePlanStaleError(
                "live database changed after restore preview"
            ) from exc
        fingerprint = self._restore_fingerprint(
            backup.sha256,
            target,
            current_exists,
            current_sha,
            current_state_sha,
        )
        if not self._equal(fingerprint, plan.confirmation_fingerprint):
            raise RestorePlanStaleError("live database changed after restore preview")
        return current_exists

    @classmethod
    def _ensure_restore_family_coherent(cls, target: Path) -> None:
        wal = cls._wal_path(target)
        shm = cls._shm_path(target)
        if cls._is_indirect_path(target):
            raise RestoreSafetyError("restore target must not be an indirect filesystem path")
        if target.exists() and not target.is_file():
            raise RestoreSafetyError("restore target is not a regular database file")
        for sidecar in (wal, shm):
            if cls._is_indirect_path(sidecar):
                raise RestoreSafetyError("SQLite restore sidecar must not be indirect")
            if sidecar.exists() and not sidecar.is_file():
                raise RestoreSafetyError("SQLite restore sidecar is not a regular file")
        if not target.exists() and (wal.exists() or shm.exists()):
            raise RestoreSafetyError(
                "live database is missing while SQLite sidecars remain"
            )

    @classmethod
    def _ensure_backup_artifact_coherent(cls, database: Path) -> None:
        wal = cls._wal_path(database)
        shm = cls._shm_path(database)
        for sidecar in (wal, shm):
            if cls._is_indirect_path(sidecar):
                raise BackupVerificationError("backup SQLite sidecar must not be indirect")
            if sidecar.exists():
                raise BackupVerificationError(
                    "backup artifact has SQLite sidecar state not bound by its manifest"
                )

    @classmethod
    def _validate_locked_backup_source(
        cls,
        artifact: BackupArtifact,
        connection: sqlite3.Connection,
    ) -> None:
        database = artifact.database_path
        if cls._is_indirect_path(database) or not database.is_file():
            raise BackupVerificationError("backup database identity changed before staging")
        wal = cls._wal_path(database)
        shm = cls._shm_path(database)
        for sidecar in (wal, shm):
            if cls._is_indirect_path(sidecar):
                raise BackupVerificationError("backup SQLite sidecar must not be indirect")
            if sidecar.exists() and not sidecar.is_file():
                raise BackupVerificationError("backup SQLite sidecar is not a regular file")
        if wal.exists() and wal.stat().st_size > 0:
            raise BackupVerificationError(
                "backup artifact has durable WAL state not bound by its manifest"
            )
        if database.stat().st_size != artifact.size_bytes:
            raise BackupVerificationError("backup database size changed before staging")
        if not cls._equal(cls._sha256_file(database), artifact.sha256):
            raise BackupVerificationError("backup database changed before staging")
        if (
            cls._validate_connection(connection, require_supported=True)
            != artifact.schema_version
        ):
            raise BackupVerificationError("backup schema version changed before staging")

    @classmethod
    def _stage_verified_backup(cls, artifact: BackupArtifact, staged: Path) -> None:
        cls._ensure_backup_artifact_coherent(artifact.database_path)
        try:
            with exclusive_sqlite_lease(artifact.database_path) as source_connection:
                cls._validate_locked_backup_source(artifact, source_connection)
                cls._copy_connection_to_database(source_connection, staged)
                cls._validate_locked_backup_source(artifact, source_connection)
        except RecoveryLeaseError as exc:
            raise BackupVerificationError(
                "backup source cannot be held stable for restore staging"
            ) from exc

    @staticmethod
    def _is_indirect_path(path: Path) -> bool:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return False
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        file_attributes = getattr(info, "st_file_attributes", 0)
        return stat.S_ISLNK(info.st_mode) or bool(file_attributes & reparse_flag)

    @classmethod
    def _restore_state_sha256(cls, target: Path) -> str:
        """Hash the logical durable main+WAL representation approved for restore.

        A missing WAL and a zero-byte WAL both represent no durable WAL frames. SQLite
        may create an empty WAL while establishing exclusive locking, so treating those
        two representations as equivalent prevents the recovery lock from invalidating
        its own confirmation. Any non-empty WAL remains byte-for-byte authority.
        """
        cls._ensure_restore_family_coherent(target)
        digest = hashlib.sha256()
        digest.update(_RESTORE_STATE_VERSION)
        main = target
        wal = cls._wal_path(target)

        digest.update(b"main\x00present\x00")
        main_size = main.stat().st_size
        digest.update(str(main_size).encode("ascii"))
        digest.update(b"\x00")
        with main.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)

        digest.update(b"wal\x00")
        if not wal.exists() or wal.stat().st_size == 0:
            digest.update(b"empty\x00")
            return digest.hexdigest()
        digest.update(b"present\x00")
        wal_size = wal.stat().st_size
        digest.update(str(wal_size).encode("ascii"))
        digest.update(b"\x00")
        with wal.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def _validate_database(cls, path: Path, *, require_supported: bool) -> int:
        try:
            conn = sqlite3.connect(path, timeout=1.0)
            try:
                conn.execute("PRAGMA query_only = ON")
                return cls._validate_connection(
                    conn,
                    require_supported=require_supported,
                )
            finally:
                conn.close()
        except BackupVerificationError:
            raise
        except (sqlite3.DatabaseError, OSError, ValueError) as exc:
            raise BackupVerificationError(
                f"SQLite database validation failed: {exc}"
            ) from exc

    @classmethod
    def _validate_connection(
        cls,
        connection: sqlite3.Connection,
        *,
        require_supported: bool,
    ) -> int:
        try:
            connection.execute("PRAGMA trusted_schema = OFF")
            integrity = tuple(
                str(row[0]) for row in connection.execute("PRAGMA integrity_check")
            )
            if integrity != ("ok",):
                detail = "; ".join(integrity[:5]) or "no integrity result"
                raise BackupVerificationError(
                    f"SQLite integrity check failed: {detail}"
                )
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise BackupVerificationError("SQLite foreign-key check failed")
            versions = tuple(
                int(row[0])
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            )
        except BackupVerificationError:
            raise
        except (sqlite3.DatabaseError, OSError, ValueError) as exc:
            raise BackupVerificationError(
                f"SQLite database validation failed: {exc}"
            ) from exc
        if not versions:
            raise BackupVerificationError("database has no applied Nika schema migrations")
        if versions != tuple(range(1, versions[-1] + 1)):
            raise BackupVerificationError("database migration history is not contiguous")
        schema = versions[-1]
        if require_supported and schema > SCHEMA_VERSION:
            raise BackupVerificationError(
                f"database schema {schema} is newer than supported schema {SCHEMA_VERSION}"
            )
        return schema

    @staticmethod
    def _copy_database(
        source: Path, destination: Path, *, overwrite: bool = False
    ) -> None:
        if not source.is_file():
            raise FileNotFoundError(f"SQLite source does not exist: {source}")
        if source.resolve() == destination.resolve():
            raise ValueError("SQLite copy source and destination must differ")
        if destination.exists() and not overwrite:
            raise FileExistsError(f"SQLite destination already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        source_conn = sqlite3.connect(source, timeout=5.0)
        target_conn = sqlite3.connect(destination, timeout=5.0)
        try:
            source_conn.execute("PRAGMA query_only = ON")
            source_conn.backup(target_conn, pages=128, sleep=0.05)
        finally:
            target_conn.close()
            source_conn.close()

    @staticmethod
    def _copy_connection_to_database(
        source_connection: sqlite3.Connection,
        destination: Path,
    ) -> None:
        if destination.exists():
            raise FileExistsError(f"SQLite destination already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        target_connection = sqlite3.connect(destination, timeout=5.0)
        try:
            source_connection.backup(target_connection, pages=128, sleep=0.05)
        finally:
            target_connection.close()

    @staticmethod
    def _copy_database_to_connection(
        source: Path,
        destination_connection: sqlite3.Connection,
    ) -> None:
        if not source.is_file():
            raise FileNotFoundError(f"SQLite source does not exist: {source}")
        source_connection = sqlite3.connect(source, timeout=5.0)
        try:
            source_connection.execute("PRAGMA query_only = ON")
            source_connection.backup(
                destination_connection,
                pages=128,
                sleep=0.05,
            )
        finally:
            source_connection.close()

    @classmethod
    def _publish_database_no_clobber(cls, staged: Path, target: Path) -> None:
        cls._ensure_restore_family_coherent(target)
        if target.exists():
            raise RestorePlanStaleError("restore target appeared before publication")
        try:
            os.link(staged, target)
        except FileExistsError as exc:
            raise RestorePlanStaleError(
                "restore target appeared before publication"
            ) from exc
        except OSError as exc:
            raise RestoreSafetyError(
                "atomic no-clobber restore publication is unavailable"
            ) from exc
        try:
            cls._fsync_directory(target.parent)
        except Exception:
            target.unlink(missing_ok=True)
            raise

    @staticmethod
    def _manifest_path(database: Path) -> Path:
        return database.with_name(f"{database.name}.manifest.json")

    @staticmethod
    def _restore_marker_path(target: Path) -> Path:
        return target.with_name(f".{target.name}.restore-in-progress.json")

    @staticmethod
    def _recovery_lease_path(target: Path) -> Path:
        return target.with_name(f".{target.name}.nika-recovery.lock")

    @staticmethod
    def _wal_path(target: Path) -> Path:
        return target.with_name(f"{target.name}-wal")

    @staticmethod
    def _shm_path(target: Path) -> Path:
        return target.with_name(f"{target.name}-shm")

    @staticmethod
    def _temporary_path(path: Path, purpose: str) -> Path:
        return path.with_name(f".{path.name}.{purpose}.{uuid4().hex}.tmp")

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BackupVerificationError(
                f"JSON recovery metadata is unreadable: {path.name}"
            ) from exc
        if not isinstance(content, dict):
            raise BackupVerificationError("JSON recovery metadata must be an object")
        return content

    @staticmethod
    def _write_json_fsync(path: Path, payload: dict[str, object]) -> None:
        encoded = (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        with path.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _fsync_file(path: Path) -> None:
        with path.open("r+b") as handle:
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _equal(left: str, right: str) -> bool:
        return hmac.compare_digest(left.encode(), right.encode())

    @staticmethod
    def _is_sha256(value: str) -> bool:
        return len(value) == 64 and all(c in "0123456789abcdef" for c in value)

    @staticmethod
    def _restore_fingerprint(
        backup_sha: str,
        target: Path,
        current_exists: bool,
        current_sha: str | None,
        current_state_sha: str | None,
    ) -> str:
        payload = json.dumps(
            {
                "backup_sha256": backup_sha,
                "current_exists": current_exists,
                "current_sha256": current_sha,
                "current_state_sha256": current_state_sha,
                "target": str(target),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _safety_backup_name(target: Path) -> str:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        return f"{target.name}.pre-restore-{timestamp}-{uuid4().hex[:8]}.sqlite3"

    @staticmethod
    def _quarantine_name(target: Path) -> str:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        return f"{target.name}.unrecoverable-{timestamp}-{uuid4().hex[:8]}.sqlite3"
