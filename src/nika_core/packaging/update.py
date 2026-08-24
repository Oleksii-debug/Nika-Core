from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from nika_core.data.schema import SCHEMA_VERSION
from nika_core.data.sqlite import SQLiteStore
from nika_core.packaging.release import (
    verify_distributable_evidence,
    verify_release_archive,
)
from nika_core.reliability.backup import (
    BackupArtifact,
    RestorePlan,
    RestorePlanStaleError,
    SQLiteRecoveryManager,
)

_SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_JOURNAL_VERSION = 1
_MANIFEST_VERSION = 2
_MANIFEST_KEYS = frozenset(
    {"manifest_version", "product", "version", "source_sha", "files"}
)
_TERMINAL_PHASES = frozenset({"completed", "rolled_back", "blocked"})


class UpdateLifecycleError(RuntimeError):
    """Base failure for local update orchestration."""


class CandidateVerificationError(UpdateLifecycleError):
    """The selected release candidate is not the exact authorized artifact."""


class ActiveUpdateError(UpdateLifecycleError):
    """Another update operation owns this installation."""


class UpdateRollbackError(UpdateLifecycleError):
    """Automatic rollback cannot be completed safely."""


class UpdatePhase(StrEnum):
    CANDIDATE_VERIFIED = "candidate_verified"
    BACKUP_CREATED = "backup_created"
    MIGRATION_STARTED = "migration_started"
    MIGRATED = "migrated"
    REPLACEMENT_STARTED = "replacement_started"
    REPLACED = "replaced"
    HEALTH_CHECKING = "health_checking"
    ROLLBACK_PACKAGE = "rollback_package"
    ROLLBACK_DATA_PREPARED = "rollback_data_prepared"
    ROLLBACK_DATA = "rollback_data"
    RESTARTING_OLD = "restarting_old"
    ROLLED_BACK = "rolled_back"
    COMPLETED = "completed"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class ReleaseIdentity:
    product: str
    version: str
    source_sha: str


@dataclass(frozen=True, slots=True)
class UpdateRequest:
    artifact_path: Path
    evidence_path: Path
    artifact_reference: str
    install_dir: Path
    database_path: Path
    expected_product: str
    expected_version: str
    expected_source_sha: str


@dataclass(frozen=True, slots=True)
class UpdateResult:
    operation_id: str
    phase: UpdatePhase
    installed: ReleaseIdentity
    pre_update_backup: Path
    rollback_package: Path
    failure_reason: str | None


@dataclass(frozen=True, slots=True)
class _Journal:
    schema_version: int
    operation_id: str
    phase: str
    install_dir: str
    database_path: str
    artifact_reference: str
    expected_product: str
    expected_version: str
    expected_source_sha: str
    installed_product: str
    installed_version: str
    installed_source_sha: str
    candidate_sha256: str
    restore_plan: dict[str, object] | None
    failure_reason: str | None
    created_at: str
    updated_at: str


class DatabaseMigratorPort(Protocol):
    def migrate(self, database_path: Path) -> None:
        """Migrate the exact live database or raise before update can continue."""


class StartupHealthPort(Protocol):
    def start_and_check(
        self,
        *,
        install_dir: Path,
        database_path: Path,
        expected: ReleaseIdentity,
    ) -> None:
        """Start/check the installed product or raise when health is not acceptable."""


class CanonicalSQLiteMigrator:
    """Adapter over the canonical SQLiteStore ordered migration owner."""

    def migrate(self, database_path: Path) -> None:
        store = SQLiteStore(database_path)
        store.initialize()
        if store.schema_version() != SCHEMA_VERSION:
            raise UpdateLifecycleError("database migration did not reach the current schema")


class _DuplicateJsonKey(ValueError):
    pass


class _UpdateFileLease:
    """Small cross-process lease for one local installation update at a time."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: Any | None = None

    def __enter__(self) -> _UpdateFileLease:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise ActiveUpdateError("another updater owns this installation") from exc
            else:
                import fcntl

                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise ActiveUpdateError("another updater owns this installation") from exc
        except Exception:
            handle.close()
            raise
        self._handle = handle
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        handle = self._handle
        self._handle = None
        if handle is not None:
            handle.close()


class WindowsUpdateLifecycle:
    """Crash-aware local package update orchestration.

    Package integrity is delegated to the canonical release-manifest/archive verifier.
    Database backup/restore is delegated to SQLiteRecoveryManager. The custom surface is
    limited to update operation identity, a durable journal, package directory replacement,
    and exact rollback sequencing.
    """

    def __init__(
        self,
        *,
        state_dir: Path,
        migrator: DatabaseMigratorPort | None = None,
        health: StartupHealthPort,
    ) -> None:
        self._state_dir = Path(state_dir).resolve()
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._migrator = migrator or CanonicalSQLiteMigrator()
        self._health = health

    def run(self, request: UpdateRequest) -> UpdateResult:
        normalized = self._normalize_request(request)
        operation_id = self._operation_id(normalized)
        with _UpdateFileLease(self._lease_path(normalized.install_dir)):
            self._reject_other_active_operations(operation_id, normalized.install_dir)
            journal = self._load_journal(operation_id)
            if journal is None:
                journal = self._begin_operation(operation_id, normalized)
            else:
                self._validate_journal_request(journal, normalized)
                self._verify_private_candidate(journal, normalized)
            return self._resume(journal, normalized)

    def _resume(self, journal: _Journal, request: UpdateRequest) -> UpdateResult:
        while True:
            phase = UpdatePhase(journal.phase)
            if phase == UpdatePhase.COMPLETED:
                return self._completed_result(journal, request)
            if phase == UpdatePhase.ROLLED_BACK:
                return self._rolled_back_result(journal, request)
            if phase == UpdatePhase.BLOCKED:
                raise UpdateRollbackError(
                    journal.failure_reason or "update rollback is blocked"
                )

            if phase == UpdatePhase.CANDIDATE_VERIFIED:
                journal = self._create_pre_update_backup(journal, request)
                continue
            if phase in {UpdatePhase.BACKUP_CREATED, UpdatePhase.MIGRATION_STARTED}:
                journal = self._migrate(journal, request)
                continue
            if phase in {
                UpdatePhase.MIGRATED,
                UpdatePhase.REPLACEMENT_STARTED,
            }:
                journal = self._replace_package(journal, request)
                continue
            if phase in {UpdatePhase.REPLACED, UpdatePhase.HEALTH_CHECKING}:
                journal = self._check_health(journal, request)
                continue
            if phase == UpdatePhase.ROLLBACK_PACKAGE:
                journal = self._rollback_package(journal, request)
                continue
            if phase == UpdatePhase.ROLLBACK_DATA_PREPARED:
                journal = self._execute_data_rollback(journal, request)
                continue
            if phase == UpdatePhase.ROLLBACK_DATA:
                journal = self._recover_or_execute_data_rollback(journal, request)
                continue
            if phase == UpdatePhase.RESTARTING_OLD:
                journal = self._restart_old(journal, request)
                continue
            raise UpdateLifecycleError(f"unsupported update phase: {phase}")

    def _begin_operation(self, operation_id: str, request: UpdateRequest) -> _Journal:
        installed = self._read_installed_identity(request.install_dir)
        if installed.product != request.expected_product:
            raise CandidateVerificationError("installed package product identity is unexpected")
        if installed.source_sha == request.expected_source_sha:
            raise CandidateVerificationError("candidate is already the installed source SHA")

        operation_root = self._operation_root(operation_id)
        operation_root.mkdir(parents=True, exist_ok=True)
        private_artifact = self._private_artifact(operation_id)
        private_evidence = self._private_evidence(operation_id)
        self._copy_private_file(request.artifact_path, private_artifact)
        self._copy_private_file(request.evidence_path, private_evidence)

        findings = verify_distributable_evidence(
            private_artifact,
            private_evidence,
            source_sha=request.expected_source_sha,
            artifact_reference=request.artifact_reference,
        )
        if findings:
            raise CandidateVerificationError(
                "candidate outer evidence failed: " + ", ".join(findings)
            )
        archive_findings = verify_release_archive(
            private_artifact,
            source_sha=request.expected_source_sha,
        )
        if archive_findings:
            raise CandidateVerificationError(
                "candidate archive verification failed: " + ", ".join(archive_findings)
            )
        candidate = self._read_archive_identity(private_artifact)
        expected = ReleaseIdentity(
            request.expected_product,
            request.expected_version,
            request.expected_source_sha,
        )
        if candidate != expected:
            raise CandidateVerificationError(
                "candidate release identity does not match authorized target"
            )

        self._extract_verified_candidate(operation_id, private_artifact)
        now = datetime.now(UTC).isoformat()
        journal = _Journal(
            schema_version=_JOURNAL_VERSION,
            operation_id=operation_id,
            phase=UpdatePhase.CANDIDATE_VERIFIED,
            install_dir=str(request.install_dir),
            database_path=str(request.database_path),
            artifact_reference=request.artifact_reference,
            expected_product=request.expected_product,
            expected_version=request.expected_version,
            expected_source_sha=request.expected_source_sha,
            installed_product=installed.product,
            installed_version=installed.version,
            installed_source_sha=installed.source_sha,
            candidate_sha256=self._sha256(private_artifact),
            restore_plan=None,
            failure_reason=None,
            created_at=now,
            updated_at=now,
        )
        journal = self._write_journal(journal)
        self._after_phase(UpdatePhase.CANDIDATE_VERIFIED)
        return journal

    def _create_pre_update_backup(
        self,
        journal: _Journal,
        request: UpdateRequest,
    ) -> _Journal:
        backup_path = self._backup_path(journal.operation_id)
        manager = self._recovery_manager(request.database_path)
        if backup_path.exists() or self._backup_manifest_path(backup_path).exists():
            artifact = manager.verify_backup(backup_path)
        else:
            artifact = manager.create_backup(backup_path)
        if artifact.database_path != backup_path:
            raise UpdateLifecycleError("canonical backup path changed unexpectedly")
        journal = self._advance(journal, UpdatePhase.BACKUP_CREATED)
        self._after_phase(UpdatePhase.BACKUP_CREATED)
        return journal

    def _migrate(self, journal: _Journal, request: UpdateRequest) -> _Journal:
        if UpdatePhase(journal.phase) == UpdatePhase.BACKUP_CREATED:
            journal = self._advance(journal, UpdatePhase.MIGRATION_STARTED)
            self._after_phase(UpdatePhase.MIGRATION_STARTED)
        try:
            self._migrator.migrate(request.database_path)
        except Exception as exc:
            return self._begin_rollback(
                journal,
                request,
                f"database migration failed: {type(exc).__name__}: {exc}",
            )
        journal = self._advance(journal, UpdatePhase.MIGRATED)
        self._after_phase(UpdatePhase.MIGRATED)
        return journal

    def _replace_package(self, journal: _Journal, request: UpdateRequest) -> _Journal:
        if UpdatePhase(journal.phase) == UpdatePhase.MIGRATED:
            journal = self._advance(journal, UpdatePhase.REPLACEMENT_STARTED)
            self._after_phase(UpdatePhase.REPLACEMENT_STARTED)

        staged = self._staged_dir(journal.operation_id, request.install_dir)
        rollback = self._rollback_dir(journal.operation_id, request.install_dir)
        expected = self._expected_identity(journal)

        if request.install_dir.exists():
            current = self._read_installed_identity(request.install_dir)
            if current == expected and rollback.is_dir():
                journal = self._advance(journal, UpdatePhase.REPLACED)
                self._after_phase(UpdatePhase.REPLACED)
                return journal
            if current.source_sha != journal.installed_source_sha:
                return self._block(
                    journal,
                    "install directory changed during package replacement",
                )
            if rollback.exists():
                return self._block(
                    journal,
                    "package rollback directory appeared before old package move",
                )
            os.replace(request.install_dir, rollback)
            self._fsync_directory(request.install_dir.parent)

        if not rollback.is_dir():
            return self._block(journal, "pre-update package is unavailable for rollback")
        if not staged.is_dir():
            return self._block(journal, "verified staged candidate is unavailable")
        if request.install_dir.exists():
            return self._block(journal, "install destination unexpectedly reappeared")

        os.replace(staged, request.install_dir)
        self._fsync_directory(request.install_dir.parent)
        if self._read_installed_identity(request.install_dir) != expected:
            return self._block(journal, "installed candidate identity changed after replacement")
        journal = self._advance(journal, UpdatePhase.REPLACED)
        self._after_phase(UpdatePhase.REPLACED)
        return journal

    def _check_health(self, journal: _Journal, request: UpdateRequest) -> _Journal:
        if UpdatePhase(journal.phase) == UpdatePhase.REPLACED:
            journal = self._advance(journal, UpdatePhase.HEALTH_CHECKING)
            self._after_phase(UpdatePhase.HEALTH_CHECKING)
        expected = self._expected_identity(journal)
        try:
            if self._read_installed_identity(request.install_dir) != expected:
                raise UpdateLifecycleError("installed candidate changed before health check")
            self._health.start_and_check(
                install_dir=request.install_dir,
                database_path=request.database_path,
                expected=expected,
            )
        except Exception as exc:
            return self._begin_rollback(
                journal,
                request,
                f"startup/health failed: {type(exc).__name__}: {exc}",
            )
        journal = self._advance(journal, UpdatePhase.COMPLETED)
        self._after_phase(UpdatePhase.COMPLETED)
        return journal

    def _begin_rollback(
        self,
        journal: _Journal,
        request: UpdateRequest,
        reason: str,
    ) -> _Journal:
        journal = replace(journal, failure_reason=reason)
        journal = self._advance(journal, UpdatePhase.ROLLBACK_PACKAGE)
        self._after_phase(UpdatePhase.ROLLBACK_PACKAGE)
        return journal

    def _rollback_package(self, journal: _Journal, request: UpdateRequest) -> _Journal:
        rollback = self._rollback_dir(journal.operation_id, request.install_dir)
        expected_old = ReleaseIdentity(
            journal.installed_product,
            journal.installed_version,
            journal.installed_source_sha,
        )
        if rollback.is_dir():
            if self._read_installed_identity(rollback) != expected_old:
                return self._block(journal, "rollback package identity is invalid")
            if request.install_dir.exists():
                current = self._read_installed_identity(request.install_dir)
                if current == expected_old:
                    return self._prepare_data_rollback(journal, request)
                failed = self._failed_dir(journal.operation_id, request.install_dir)
                if failed.exists():
                    return self._block(journal, "failed-candidate evidence path already exists")
                os.replace(request.install_dir, failed)
            os.replace(rollback, request.install_dir)
            self._fsync_directory(request.install_dir.parent)
        elif not request.install_dir.is_dir():
            return self._block(journal, "pre-update package is missing during rollback")

        if self._read_installed_identity(request.install_dir) != expected_old:
            return self._block(journal, "old package was not restored exactly")
        return self._prepare_data_rollback(journal, request)

    def _prepare_data_rollback(
        self,
        journal: _Journal,
        request: UpdateRequest,
    ) -> _Journal:
        if journal.restore_plan is not None:
            return self._advance(journal, UpdatePhase.ROLLBACK_DATA_PREPARED)
        manager = self._recovery_manager(request.database_path)
        backup = manager.verify_backup(self._backup_path(journal.operation_id))
        plan = manager.prepare_restore(backup.database_path)
        restore_plan = {
            "backup_sha256": backup.sha256,
            "current_exists": plan.current_exists,
            "current_sha256": plan.current_sha256,
            "current_schema_version": plan.current_schema_version,
            "current_is_healthy": plan.current_is_healthy,
            "confirmation_fingerprint": plan.confirmation_fingerprint,
        }
        journal = replace(journal, restore_plan=restore_plan)
        journal = self._advance(journal, UpdatePhase.ROLLBACK_DATA_PREPARED)
        self._after_phase(UpdatePhase.ROLLBACK_DATA_PREPARED)
        return journal

    def _execute_data_rollback(
        self,
        journal: _Journal,
        request: UpdateRequest,
    ) -> _Journal:
        journal = self._advance(journal, UpdatePhase.ROLLBACK_DATA)
        self._after_phase(UpdatePhase.ROLLBACK_DATA)
        return self._recover_or_execute_data_rollback(journal, request)

    def _recover_or_execute_data_rollback(
        self,
        journal: _Journal,
        request: UpdateRequest,
    ) -> _Journal:
        manager = self._recovery_manager(request.database_path)
        recovered = manager.recover_interrupted_restore()
        if recovered is None:
            plan = self._restore_plan_from_journal(journal, request, manager)
            try:
                manager.restore(
                    plan,
                    confirmation_fingerprint=plan.confirmation_fingerprint,
                    allow_replace_unrecoverable_current=not plan.current_is_healthy,
                )
            except RestorePlanStaleError as exc:
                return self._block(
                    journal,
                    "rollback confirmation became stale; refusing to re-authorize restore: "
                    f"{exc}",
                )
            except Exception as exc:
                return self._block(
                    journal,
                    f"canonical data rollback failed: {type(exc).__name__}: {exc}",
                )
        journal = self._advance(journal, UpdatePhase.RESTARTING_OLD)
        self._after_phase(UpdatePhase.RESTARTING_OLD)
        return journal

    def _restart_old(self, journal: _Journal, request: UpdateRequest) -> _Journal:
        expected_old = ReleaseIdentity(
            journal.installed_product,
            journal.installed_version,
            journal.installed_source_sha,
        )
        try:
            if self._read_installed_identity(request.install_dir) != expected_old:
                raise UpdateRollbackError("old package identity changed before restart")
            self._health.start_and_check(
                install_dir=request.install_dir,
                database_path=request.database_path,
                expected=expected_old,
            )
        except Exception as exc:
            return self._block(
                journal,
                f"old package restart/health failed after rollback: {type(exc).__name__}: {exc}",
            )
        journal = self._advance(journal, UpdatePhase.ROLLED_BACK)
        self._after_phase(UpdatePhase.ROLLED_BACK)
        return journal

    def _restore_plan_from_journal(
        self,
        journal: _Journal,
        request: UpdateRequest,
        manager: SQLiteRecoveryManager,
    ) -> RestorePlan:
        payload = journal.restore_plan
        if payload is None:
            raise UpdateRollbackError("rollback restore plan is missing from durable journal")
        expected_keys = {
            "backup_sha256",
            "current_exists",
            "current_sha256",
            "current_schema_version",
            "current_is_healthy",
            "confirmation_fingerprint",
        }
        if set(payload) != expected_keys:
            raise UpdateRollbackError("rollback restore plan has an invalid schema")
        backup = manager.verify_backup(self._backup_path(journal.operation_id))
        backup_sha = payload["backup_sha256"]
        fingerprint = payload["confirmation_fingerprint"]
        if backup_sha != backup.sha256 or not self._valid_sha256(backup_sha):
            raise UpdateRollbackError("rollback backup identity no longer matches journal")
        if not self._valid_sha256(fingerprint):
            raise UpdateRollbackError("rollback confirmation fingerprint is malformed")
        current_exists = payload["current_exists"]
        current_is_healthy = payload["current_is_healthy"]
        if type(current_exists) is not bool or type(current_is_healthy) is not bool:
            raise UpdateRollbackError("rollback restore booleans are malformed")
        current_sha = payload["current_sha256"]
        if current_sha is not None and not self._valid_sha256(current_sha):
            raise UpdateRollbackError("rollback current SHA-256 is malformed")
        schema = payload["current_schema_version"]
        if schema is not None and (type(schema) is not int or schema < 0):
            raise UpdateRollbackError("rollback current schema version is malformed")
        return RestorePlan(
            backup=backup,
            target_path=request.database_path,
            current_exists=current_exists,
            current_sha256=current_sha,
            current_schema_version=schema,
            current_is_healthy=current_is_healthy,
            confirmation_fingerprint=fingerprint,
        )

    def _completed_result(self, journal: _Journal, request: UpdateRequest) -> UpdateResult:
        expected = self._expected_identity(journal)
        if self._read_installed_identity(request.install_dir) != expected:
            raise UpdateLifecycleError("completed update no longer matches installed package")
        return self._result(journal, expected)

    def _rolled_back_result(self, journal: _Journal, request: UpdateRequest) -> UpdateResult:
        expected = ReleaseIdentity(
            journal.installed_product,
            journal.installed_version,
            journal.installed_source_sha,
        )
        if self._read_installed_identity(request.install_dir) != expected:
            raise UpdateRollbackError("rolled-back update no longer matches old package")
        return self._result(journal, expected)

    def _result(self, journal: _Journal, installed: ReleaseIdentity) -> UpdateResult:
        return UpdateResult(
            operation_id=journal.operation_id,
            phase=UpdatePhase(journal.phase),
            installed=installed,
            pre_update_backup=self._backup_path(journal.operation_id),
            rollback_package=self._rollback_dir(
                journal.operation_id,
                Path(journal.install_dir),
            ),
            failure_reason=journal.failure_reason,
        )

    def _verify_private_candidate(self, journal: _Journal, request: UpdateRequest) -> None:
        artifact = self._private_artifact(journal.operation_id)
        evidence = self._private_evidence(journal.operation_id)
        if not artifact.is_file() or not evidence.is_file():
            raise CandidateVerificationError("durable private candidate evidence is missing")
        if self._sha256(artifact) != journal.candidate_sha256:
            raise CandidateVerificationError("durable private candidate bytes changed")
        findings = verify_distributable_evidence(
            artifact,
            evidence,
            source_sha=request.expected_source_sha,
            artifact_reference=request.artifact_reference,
        )
        if findings:
            raise CandidateVerificationError(
                "durable candidate outer evidence failed: " + ", ".join(findings)
            )
        findings = verify_release_archive(artifact, source_sha=request.expected_source_sha)
        if findings:
            raise CandidateVerificationError(
                "durable candidate archive verification failed: " + ", ".join(findings)
            )

    def _validate_journal_request(self, journal: _Journal, request: UpdateRequest) -> None:
        expected = (
            str(request.install_dir),
            str(request.database_path),
            request.artifact_reference,
            request.expected_product,
            request.expected_version,
            request.expected_source_sha,
        )
        actual = (
            journal.install_dir,
            journal.database_path,
            journal.artifact_reference,
            journal.expected_product,
            journal.expected_version,
            journal.expected_source_sha,
        )
        if actual != expected:
            raise UpdateLifecycleError("durable update journal belongs to another request")

    def _reject_other_active_operations(self, operation_id: str, install_dir: Path) -> None:
        prefix = self._journal_prefix(install_dir)
        for path in self._state_dir.glob(f"{prefix}-*.json"):
            journal = self._read_journal(path)
            if journal.operation_id != operation_id and journal.phase not in _TERMINAL_PHASES:
                raise ActiveUpdateError(
                    f"another non-terminal update operation exists: {journal.operation_id}"
                )

    def _normalize_request(self, request: UpdateRequest) -> UpdateRequest:
        artifact = Path(request.artifact_path).resolve(strict=True)
        evidence = Path(request.evidence_path).resolve(strict=True)
        install = Path(request.install_dir).resolve(strict=True)
        database = Path(request.database_path).resolve(strict=True)
        if not artifact.is_file() or not evidence.is_file():
            raise CandidateVerificationError("candidate artifact and evidence must be files")
        if not install.is_dir():
            raise UpdateLifecycleError("install_dir must be the installed package directory")
        if not database.is_file():
            raise UpdateLifecycleError("database_path must be the durable live database")
        source_sha = request.expected_source_sha.strip().lower()
        if not _SOURCE_SHA_RE.fullmatch(source_sha):
            raise CandidateVerificationError("authorized target source SHA is invalid")
        product = request.expected_product.strip()
        version = request.expected_version.strip()
        artifact_reference = request.artifact_reference.strip()
        if not product or not version or not artifact_reference:
            raise CandidateVerificationError("authorized candidate identity is incomplete")
        return UpdateRequest(
            artifact_path=artifact,
            evidence_path=evidence,
            artifact_reference=artifact_reference,
            install_dir=install,
            database_path=database,
            expected_product=product,
            expected_version=version,
            expected_source_sha=source_sha,
        )

    def _operation_id(self, request: UpdateRequest) -> str:
        payload = json.dumps(
            {
                "database_path": str(request.database_path),
                "install_dir": str(request.install_dir),
                "product": request.expected_product,
                "source_sha": request.expected_source_sha,
                "version": request.expected_version,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:32]

    def _advance(self, journal: _Journal, phase: UpdatePhase) -> _Journal:
        updated = replace(
            journal,
            phase=phase,
            updated_at=datetime.now(UTC).isoformat(),
        )
        return self._write_journal(updated)

    def _block(self, journal: _Journal, reason: str) -> _Journal:
        blocked = replace(
            journal,
            phase=UpdatePhase.BLOCKED,
            failure_reason=reason,
            updated_at=datetime.now(UTC).isoformat(),
        )
        blocked = self._write_journal(blocked)
        self._after_phase(UpdatePhase.BLOCKED)
        return blocked

    def _write_journal(self, journal: _Journal) -> _Journal:
        path = self._journal_path(journal.operation_id, Path(journal.install_dir))
        payload = asdict(journal)
        serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._state_dir,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
                temporary = Path(handle.name)
            os.replace(temporary, path)
            self._fsync_directory(path.parent)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return journal

    def _load_journal(self, operation_id: str) -> _Journal | None:
        matches = tuple(self._state_dir.glob(f"*-{operation_id}.json"))
        if not matches:
            return None
        if len(matches) != 1:
            raise UpdateLifecycleError("duplicate durable journals exist for one operation")
        return self._read_journal(matches[0])

    def _read_journal(self, path: Path) -> _Journal:
        try:
            payload = json.loads(
                path.read_text(encoding="utf-8-sig"),
                object_pairs_hook=self._unique_json_object,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, _DuplicateJsonKey) as exc:
            raise UpdateLifecycleError("durable update journal is malformed") from exc
        if not isinstance(payload, dict) or set(payload) != set(_Journal.__annotations__):
            raise UpdateLifecycleError("durable update journal schema is invalid")
        if payload.get("schema_version") != _JOURNAL_VERSION:
            raise UpdateLifecycleError("unsupported durable update journal version")
        try:
            journal = _Journal(**payload)
            UpdatePhase(journal.phase)
        except (TypeError, ValueError) as exc:
            raise UpdateLifecycleError("durable update journal values are invalid") from exc
        for source_sha in (journal.expected_source_sha, journal.installed_source_sha):
            if not _SOURCE_SHA_RE.fullmatch(source_sha):
                raise UpdateLifecycleError("durable update journal source SHA is invalid")
        if not self._valid_sha256(journal.candidate_sha256):
            raise UpdateLifecycleError("durable update journal candidate digest is invalid")
        if journal.failure_reason is not None and not isinstance(journal.failure_reason, str):
            raise UpdateLifecycleError("durable update journal failure reason is invalid")
        return journal

    def _extract_verified_candidate(self, operation_id: str, artifact: Path) -> None:
        staged = self._staged_dir(operation_id, self._install_path_from_operation(operation_id))
        if staged.exists():
            identity = self._read_installed_identity(staged)
            if identity.source_sha != self._read_archive_identity(artifact).source_sha:
                raise CandidateVerificationError("existing staged candidate has another identity")
            return
        staged.parent.mkdir(parents=True, exist_ok=True)
        temporary = staged.with_name(f"{staged.name}.extracting")
        if temporary.exists():
            shutil.rmtree(temporary)
        temporary.mkdir(parents=False)
        try:
            with zipfile.ZipFile(artifact, "r") as archive:
                archive.extractall(temporary)
            if self._read_installed_identity(temporary) != self._read_archive_identity(artifact):
                raise CandidateVerificationError("extracted candidate identity changed")
            os.replace(temporary, staged)
            self._fsync_directory(staged.parent)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def _install_path_from_operation(self, operation_id: str) -> Path:
        journal = self._load_journal(operation_id)
        if journal is not None:
            return Path(journal.install_dir)
        marker = self._operation_root(operation_id) / "install-path.txt"
        if not marker.is_file():
            raise UpdateLifecycleError("operation install path marker is missing")
        return Path(marker.read_text(encoding="utf-8")).resolve()

    def _copy_private_file(self, source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            return
        temporary = destination.with_name(f".{destination.name}.copying")
        shutil.copyfile(source, temporary)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        self._fsync_directory(destination.parent)

    def _read_installed_identity(self, install_dir: Path) -> ReleaseIdentity:
        manifest = install_dir / "release-manifest.json"
        if not manifest.is_file():
            raise CandidateVerificationError(f"release manifest is missing: {manifest}")
        try:
            payload = json.loads(
                manifest.read_text(encoding="utf-8-sig"),
                object_pairs_hook=self._unique_json_object,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, _DuplicateJsonKey) as exc:
            raise CandidateVerificationError("installed release manifest is invalid") from exc
        return self._identity_from_manifest_payload(payload)

    def _read_archive_identity(self, artifact: Path) -> ReleaseIdentity:
        try:
            with zipfile.ZipFile(artifact, "r") as archive:
                content = archive.read("release-manifest.json").decode("utf-8-sig")
        except (OSError, UnicodeError, KeyError, zipfile.BadZipFile) as exc:
            raise CandidateVerificationError("candidate release manifest cannot be read") from exc
        try:
            payload = json.loads(content, object_pairs_hook=self._unique_json_object)
        except (json.JSONDecodeError, _DuplicateJsonKey) as exc:
            raise CandidateVerificationError("candidate release manifest is invalid") from exc
        return self._identity_from_manifest_payload(payload)

    @staticmethod
    def _identity_from_manifest_payload(payload: object) -> ReleaseIdentity:
        if not isinstance(payload, dict) or frozenset(payload) != _MANIFEST_KEYS:
            raise CandidateVerificationError("release manifest schema is invalid")
        if payload.get("manifest_version") != _MANIFEST_VERSION:
            raise CandidateVerificationError("release manifest version is unsupported")
        product = payload.get("product")
        version = payload.get("version")
        source_sha = payload.get("source_sha")
        if (
            not isinstance(product, str)
            or not product
            or product != product.strip()
            or not isinstance(version, str)
            or not version
            or version != version.strip()
            or not isinstance(source_sha, str)
            or not _SOURCE_SHA_RE.fullmatch(source_sha)
        ):
            raise CandidateVerificationError("release manifest identity is malformed")
        if not isinstance(payload.get("files"), list) or not payload["files"]:
            raise CandidateVerificationError("release manifest file inventory is invalid")
        return ReleaseIdentity(product, version, source_sha)

    @staticmethod
    def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise _DuplicateJsonKey(key)
            result[key] = value
        return result

    @staticmethod
    def _expected_identity(journal: _Journal) -> ReleaseIdentity:
        return ReleaseIdentity(
            journal.expected_product,
            journal.expected_version,
            journal.expected_source_sha,
        )

    def _recovery_manager(self, database_path: Path) -> SQLiteRecoveryManager:
        return SQLiteRecoveryManager(SQLiteStore(database_path))

    def _operation_root(self, operation_id: str) -> Path:
        return self._state_dir / "operations" / operation_id

    def _private_artifact(self, operation_id: str) -> Path:
        return self._operation_root(operation_id) / "candidate.zip"

    def _private_evidence(self, operation_id: str) -> Path:
        return self._operation_root(operation_id) / "candidate-evidence.json"

    def _backup_path(self, operation_id: str) -> Path:
        return self._operation_root(operation_id) / "pre-update.sqlite3"

    @staticmethod
    def _backup_manifest_path(backup_path: Path) -> Path:
        return backup_path.with_name(f"{backup_path.name}.manifest.json")

    @staticmethod
    def _staged_dir(operation_id: str, install_dir: Path) -> Path:
        return install_dir.parent / f".{install_dir.name}.nika-update-{operation_id}.stage"

    @staticmethod
    def _rollback_dir(operation_id: str, install_dir: Path) -> Path:
        return install_dir.parent / f".{install_dir.name}.nika-update-{operation_id}.rollback"

    @staticmethod
    def _failed_dir(operation_id: str, install_dir: Path) -> Path:
        return install_dir.parent / f".{install_dir.name}.nika-update-{operation_id}.failed"

    def _journal_prefix(self, install_dir: Path) -> str:
        digest = hashlib.sha256(str(install_dir).encode("utf-8")).hexdigest()[:16]
        return f"update-{digest}"

    def _journal_path(self, operation_id: str, install_dir: Path) -> Path:
        return self._state_dir / f"{self._journal_prefix(install_dir)}-{operation_id}.json"

    def _lease_path(self, install_dir: Path) -> Path:
        return self._state_dir / f"{self._journal_prefix(install_dir)}.lock"

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _valid_sha256(value: object) -> bool:
        return isinstance(value, str) and bool(_SHA256_RE.fullmatch(value))

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _after_phase(self, phase: UpdatePhase) -> None:
        """Fault-injection seam for deterministic crash tests; production is a no-op."""
        del phase
