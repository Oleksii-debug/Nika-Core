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
from nika_core.kernel.audit import AuditLog
from nika_core.packaging.release import (
    verify_distributable_evidence,
    verify_release_archive,
)
from nika_core.reliability.backup import (
    InterruptedRestoreDisposition,
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
    """The candidate is not the exact release authorized by the host."""


class ActiveUpdateError(UpdateLifecycleError):
    """Another update owns the same installation."""


class UpdateRollbackError(UpdateLifecycleError):
    """Rollback cannot safely continue without new operator action."""


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
        """Apply the candidate's ordered durable migrations or raise."""


class StartupHealthPort(Protocol):
    def start_and_check(
        self,
        *,
        install_dir: Path,
        database_path: Path,
        expected: ReleaseIdentity,
    ) -> None:
        """Start/reconcile the installed application and raise if unhealthy."""


class CanonicalSQLiteMigrator:
    """Thin adapter over SQLiteStore's canonical ordered migration owner."""

    def migrate(self, database_path: Path) -> None:
        store = SQLiteStore(database_path)
        store.initialize()
        if store.schema_version() != SCHEMA_VERSION:
            raise UpdateLifecycleError("database migration did not reach current schema")


class _DuplicateJsonKey(ValueError):
    pass


class _UpdateFileLease:
    """OS-released non-blocking lease; the durable journal carries operation state."""

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
    """One durable local package-update operation, not a package manager.

    Release ZIP authority remains in packaging.release. SQLite backup, restore,
    stale-confirmation checks and interrupted-restore reconciliation remain in
    SQLiteRecoveryManager. This class adds only operation sequencing, the external
    package-directory swap, a durable receipt journal and a host health boundary.
    """

    def __init__(
        self,
        *,
        state_dir: Path,
        health: StartupHealthPort,
        migrator: DatabaseMigratorPort | None = None,
    ) -> None:
        self._state_dir = Path(state_dir).resolve()
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._health = health
        self._migrator = migrator or CanonicalSQLiteMigrator()

    def run(self, request: UpdateRequest) -> UpdateResult:
        request = self._normalize_request(request)
        operation_id = self._operation_id(request)
        with _UpdateFileLease(self._lease_path(request.install_dir)):
            self._reject_other_active(operation_id, request.install_dir)
            journal = self._load_journal(operation_id, request.install_dir)
            if journal is None:
                journal = self._begin(operation_id, request)
            else:
                self._validate_journal_request(journal, request)
                self._verify_private_candidate(journal, request)
            return self._resume(journal, request)

    def _resume(self, journal: _Journal, request: UpdateRequest) -> UpdateResult:
        while True:
            phase = UpdatePhase(journal.phase)
            if phase == UpdatePhase.COMPLETED:
                return self._terminal_result(journal, request, new=True)
            if phase == UpdatePhase.ROLLED_BACK:
                return self._terminal_result(journal, request, new=False)
            if phase == UpdatePhase.BLOCKED:
                raise UpdateRollbackError(journal.failure_reason or "update rollback is blocked")
            if phase == UpdatePhase.CANDIDATE_VERIFIED:
                journal = self._backup(journal, request)
            elif phase in {UpdatePhase.BACKUP_CREATED, UpdatePhase.MIGRATION_STARTED}:
                journal = self._migrate(journal, request)
            elif phase in {UpdatePhase.MIGRATED, UpdatePhase.REPLACEMENT_STARTED}:
                journal = self._replace_package(journal, request)
            elif phase in {UpdatePhase.REPLACED, UpdatePhase.HEALTH_CHECKING}:
                journal = self._health_check(journal, request)
            elif phase == UpdatePhase.ROLLBACK_PACKAGE:
                journal = self._rollback_package(journal, request)
            elif phase == UpdatePhase.ROLLBACK_DATA_PREPARED:
                journal = self._execute_data_rollback(journal, request)
            elif phase == UpdatePhase.ROLLBACK_DATA:
                journal = self._recover_or_restore_data(journal, request)
            elif phase == UpdatePhase.RESTARTING_OLD:
                journal = self._restart_old(journal, request)
            else:
                raise UpdateLifecycleError(f"unsupported update phase: {phase}")

    def _begin(self, operation_id: str, request: UpdateRequest) -> _Journal:
        if not request.artifact_path.is_file() or not request.evidence_path.is_file():
            raise CandidateVerificationError("candidate artifact/evidence is missing")
        if not request.install_dir.is_dir():
            raise UpdateLifecycleError("installed package directory is missing")
        if not request.database_path.is_file():
            raise UpdateLifecycleError("durable database is missing before update")

        old = self._read_installed_identity(request.install_dir)
        if old.product != request.expected_product:
            raise CandidateVerificationError("installed package product identity is unexpected")
        if old.source_sha == request.expected_source_sha:
            raise CandidateVerificationError("candidate is already the installed source SHA")

        root = self._operation_root(operation_id)
        root.mkdir(parents=True, exist_ok=True)
        artifact = self._private_artifact(operation_id)
        evidence = self._private_evidence(operation_id)
        self._copy_private(request.artifact_path, artifact)
        self._copy_private(request.evidence_path, evidence)
        self._verify_candidate_files(artifact, evidence, request)
        identity = self._read_archive_identity(artifact)
        if identity != self._new_identity_from_request(request):
            raise CandidateVerificationError(
                "candidate release identity does not match authorized target"
            )
        self._extract_candidate(operation_id, artifact, request.install_dir)

        now = datetime.now(UTC).isoformat()
        journal = _Journal(
            schema_version=_JOURNAL_VERSION,
            operation_id=operation_id,
            phase=UpdatePhase.CANDIDATE_VERIFIED.value,
            install_dir=str(request.install_dir),
            database_path=str(request.database_path),
            artifact_reference=request.artifact_reference,
            expected_product=request.expected_product,
            expected_version=request.expected_version,
            expected_source_sha=request.expected_source_sha,
            installed_product=old.product,
            installed_version=old.version,
            installed_source_sha=old.source_sha,
            candidate_sha256=self._sha256(artifact),
            restore_plan=None,
            failure_reason=None,
            created_at=now,
            updated_at=now,
        )
        journal = self._write_journal(journal)
        self._after_phase(UpdatePhase.CANDIDATE_VERIFIED)
        return journal

    def _backup(self, journal: _Journal, request: UpdateRequest) -> _Journal:
        path = self._backup_path(journal.operation_id)
        manager = self._manager(request.database_path)
        if path.exists() or self._backup_manifest(path).exists():
            artifact = manager.verify_backup(path)
        else:
            artifact = manager.create_backup(path)
        if artifact.database_path != path:
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
            return self._start_rollback(
                journal,
                f"database migration failed: {type(exc).__name__}: {exc}",
            )
        journal = self._advance(journal, UpdatePhase.MIGRATED)
        self._after_phase(UpdatePhase.MIGRATED)
        return journal

    def _replace_package(self, journal: _Journal, request: UpdateRequest) -> _Journal:
        if UpdatePhase(journal.phase) == UpdatePhase.MIGRATED:
            journal = self._advance(journal, UpdatePhase.REPLACEMENT_STARTED)
            self._after_phase(UpdatePhase.REPLACEMENT_STARTED)

        staged = self._staged(journal.operation_id, request.install_dir)
        rollback = self._rollback(journal.operation_id, request.install_dir)
        old = self._old_identity(journal)
        new = self._new_identity(journal)

        if request.install_dir.exists():
            current = self._read_installed_identity(request.install_dir)
            if current == new and rollback.is_dir():
                return self._phase_replaced(journal)
            if current != old:
                return self._block(journal, "install directory changed during replacement")
            if rollback.exists():
                return self._block(journal, "rollback path appeared before old package move")
            os.replace(request.install_dir, rollback)
            self._fsync_dir(request.install_dir.parent)

        if not rollback.is_dir() or self._read_installed_identity(rollback) != old:
            return self._block(journal, "pre-update package is unavailable or changed")
        if not staged.is_dir():
            return self._block(journal, "verified staged candidate is unavailable")
        if request.install_dir.exists():
            return self._block(journal, "install destination unexpectedly reappeared")

        os.replace(staged, request.install_dir)
        self._fsync_dir(request.install_dir.parent)
        if self._read_installed_identity(request.install_dir) != new:
            return self._block(journal, "installed candidate identity changed after replacement")
        return self._phase_replaced(journal)

    def _phase_replaced(self, journal: _Journal) -> _Journal:
        journal = self._advance(journal, UpdatePhase.REPLACED)
        self._after_phase(UpdatePhase.REPLACED)
        return journal

    def _health_check(self, journal: _Journal, request: UpdateRequest) -> _Journal:
        if UpdatePhase(journal.phase) == UpdatePhase.REPLACED:
            journal = self._advance(journal, UpdatePhase.HEALTH_CHECKING)
            self._after_phase(UpdatePhase.HEALTH_CHECKING)
        new = self._new_identity(journal)
        try:
            if self._read_installed_identity(request.install_dir) != new:
                raise UpdateLifecycleError("installed candidate changed before health check")
            self._health.start_and_check(
                install_dir=request.install_dir,
                database_path=request.database_path,
                expected=new,
            )
        except Exception as exc:
            return self._start_rollback(
                journal,
                f"startup/health failed: {type(exc).__name__}: {exc}",
            )
        journal = self._advance(journal, UpdatePhase.COMPLETED)
        self._after_phase(UpdatePhase.COMPLETED)
        return journal

    def _start_rollback(self, journal: _Journal, reason: str) -> _Journal:
        journal = replace(journal, failure_reason=reason)
        journal = self._advance(journal, UpdatePhase.ROLLBACK_PACKAGE)
        self._after_phase(UpdatePhase.ROLLBACK_PACKAGE)
        return journal

    def _rollback_package(self, journal: _Journal, request: UpdateRequest) -> _Journal:
        rollback = self._rollback(journal.operation_id, request.install_dir)
        old = self._old_identity(journal)
        if rollback.is_dir():
            if self._read_installed_identity(rollback) != old:
                return self._block(journal, "rollback package identity is invalid")
            if request.install_dir.exists():
                if self._read_installed_identity(request.install_dir) == old:
                    return self._prepare_data_rollback(journal, request)
                failed = self._failed(journal.operation_id, request.install_dir)
                if failed.exists():
                    return self._block(journal, "failed candidate evidence already exists")
                os.replace(request.install_dir, failed)
            os.replace(rollback, request.install_dir)
            self._fsync_dir(request.install_dir.parent)
        elif not request.install_dir.is_dir():
            return self._block(journal, "pre-update package is missing during rollback")
        if self._read_installed_identity(request.install_dir) != old:
            return self._block(journal, "old package was not restored exactly")
        return self._prepare_data_rollback(journal, request)

    def _prepare_data_rollback(
        self,
        journal: _Journal,
        request: UpdateRequest,
    ) -> _Journal:
        if journal.restore_plan is None:
            manager = self._manager(request.database_path)
            backup = manager.verify_backup(self._backup_path(journal.operation_id))
            plan = manager.prepare_restore(backup.database_path)
            journal = replace(
                journal,
                restore_plan={
                    "backup_sha256": backup.sha256,
                    "current_exists": plan.current_exists,
                    "current_sha256": plan.current_sha256,
                    "current_schema_version": plan.current_schema_version,
                    "current_is_healthy": plan.current_is_healthy,
                    "confirmation_fingerprint": plan.confirmation_fingerprint,
                },
            )
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
        return self._recover_or_restore_data(journal, request)

    def _recover_or_restore_data(
        self,
        journal: _Journal,
        request: UpdateRequest,
    ) -> _Journal:
        manager = self._manager(request.database_path)
        if self._restore_receipt_exists(journal, request.database_path):
            return self._phase_restarting_old(journal)

        recovered = manager.recover_interrupted_restore()
        if recovered is not None:
            if recovered.disposition == InterruptedRestoreDisposition.COMPLETED:
                if not self._restore_receipt_exists(journal, request.database_path):
                    return self._block(
                        journal,
                        "interrupted restore completed without bound audit receipt",
                    )
                return self._phase_restarting_old(journal)
            if recovered.disposition != InterruptedRestoreDisposition.ROLLED_BACK:
                return self._block(journal, "unknown interrupted restore disposition")

        plan = self._restore_plan(journal, request.database_path, manager)
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
        if not self._restore_receipt_exists(journal, request.database_path):
            return self._block(journal, "canonical data rollback lacks durable audit receipt")
        return self._phase_restarting_old(journal)

    def _phase_restarting_old(self, journal: _Journal) -> _Journal:
        journal = self._advance(journal, UpdatePhase.RESTARTING_OLD)
        self._after_phase(UpdatePhase.RESTARTING_OLD)
        return journal

    def _restart_old(self, journal: _Journal, request: UpdateRequest) -> _Journal:
        old = self._old_identity(journal)
        try:
            if self._read_installed_identity(request.install_dir) != old:
                raise UpdateRollbackError("old package changed before rollback restart")
            self._health.start_and_check(
                install_dir=request.install_dir,
                database_path=request.database_path,
                expected=old,
            )
        except Exception as exc:
            return self._block(
                journal,
                "old package restart/health failed after rollback: "
                f"{type(exc).__name__}: {exc}",
            )
        journal = self._advance(journal, UpdatePhase.ROLLED_BACK)
        self._after_phase(UpdatePhase.ROLLED_BACK)
        return journal

    def _restore_receipt_exists(self, journal: _Journal, database_path: Path) -> bool:
        if not database_path.is_file() or journal.restore_plan is None:
            return False
        backup_sha = journal.restore_plan.get("backup_sha256")
        if not self._valid_sha256(backup_sha):
            return False
        backup_name = self._backup_path(journal.operation_id).name
        events = AuditLog(SQLiteStore(database_path)).list_for(
            entity_type="database",
            entity_id=str(database_path),
        )
        return any(
            event.event_type == "reliability.restore_completed"
            and event.payload.get("backup_file") == backup_name
            and event.payload.get("backup_sha256") == backup_sha
            for event in events
        )

    def _restore_plan(
        self,
        journal: _Journal,
        database_path: Path,
        manager: SQLiteRecoveryManager,
    ) -> RestorePlan:
        payload = journal.restore_plan
        keys = {
            "backup_sha256",
            "current_exists",
            "current_sha256",
            "current_schema_version",
            "current_is_healthy",
            "confirmation_fingerprint",
        }
        if payload is None or set(payload) != keys:
            raise UpdateRollbackError("rollback restore plan has invalid schema")
        backup = manager.verify_backup(self._backup_path(journal.operation_id))
        if payload["backup_sha256"] != backup.sha256:
            raise UpdateRollbackError("rollback backup identity no longer matches journal")
        fingerprint = payload["confirmation_fingerprint"]
        current_sha = payload["current_sha256"]
        current_exists = payload["current_exists"]
        current_healthy = payload["current_is_healthy"]
        schema = payload["current_schema_version"]
        if not self._valid_sha256(fingerprint):
            raise UpdateRollbackError("rollback confirmation fingerprint is malformed")
        if current_sha is not None and not self._valid_sha256(current_sha):
            raise UpdateRollbackError("rollback current SHA-256 is malformed")
        if type(current_exists) is not bool or type(current_healthy) is not bool:
            raise UpdateRollbackError("rollback restore booleans are malformed")
        if schema is not None and (type(schema) is not int or schema < 0):
            raise UpdateRollbackError("rollback current schema version is malformed")
        return RestorePlan(
            backup=backup,
            target_path=database_path,
            current_exists=current_exists,
            current_sha256=current_sha,
            current_schema_version=schema,
            current_is_healthy=current_healthy,
            confirmation_fingerprint=fingerprint,
        )

    def _terminal_result(
        self,
        journal: _Journal,
        request: UpdateRequest,
        *,
        new: bool,
    ) -> UpdateResult:
        identity = self._new_identity(journal) if new else self._old_identity(journal)
        if self._read_installed_identity(request.install_dir) != identity:
            raise UpdateLifecycleError("terminal journal no longer matches installed package")
        return UpdateResult(
            operation_id=journal.operation_id,
            phase=UpdatePhase(journal.phase),
            installed=identity,
            pre_update_backup=self._backup_path(journal.operation_id),
            rollback_package=self._rollback(journal.operation_id, request.install_dir),
            failure_reason=journal.failure_reason,
        )

    def _verify_private_candidate(self, journal: _Journal, request: UpdateRequest) -> None:
        artifact = self._private_artifact(journal.operation_id)
        evidence = self._private_evidence(journal.operation_id)
        if not artifact.is_file() or not evidence.is_file():
            raise CandidateVerificationError("durable private candidate evidence is missing")
        if self._sha256(artifact) != journal.candidate_sha256:
            raise CandidateVerificationError("durable private candidate bytes changed")
        self._verify_candidate_files(artifact, evidence, request)

    def _verify_candidate_files(
        self,
        artifact: Path,
        evidence: Path,
        request: UpdateRequest,
    ) -> None:
        findings = verify_distributable_evidence(
            artifact,
            evidence,
            source_sha=request.expected_source_sha,
            artifact_reference=request.artifact_reference,
        )
        if findings:
            raise CandidateVerificationError(
                "candidate outer evidence failed: " + ", ".join(findings)
            )
        findings = verify_release_archive(artifact, source_sha=request.expected_source_sha)
        if findings:
            raise CandidateVerificationError(
                "candidate archive verification failed: " + ", ".join(findings)
            )

    def _normalize_request(self, request: UpdateRequest) -> UpdateRequest:
        source_sha = request.expected_source_sha.strip().lower()
        product = request.expected_product.strip()
        version = request.expected_version.strip()
        reference = request.artifact_reference.strip()
        if not _SOURCE_SHA_RE.fullmatch(source_sha):
            raise CandidateVerificationError("authorized target source SHA is invalid")
        if not product or not version or not reference:
            raise CandidateVerificationError("authorized candidate identity is incomplete")
        return UpdateRequest(
            artifact_path=Path(request.artifact_path).resolve(),
            evidence_path=Path(request.evidence_path).resolve(),
            artifact_reference=reference,
            install_dir=Path(request.install_dir).resolve(),
            database_path=Path(request.database_path).resolve(),
            expected_product=product,
            expected_version=version,
            expected_source_sha=source_sha,
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

    def _operation_id(self, request: UpdateRequest) -> str:
        payload = json.dumps(
            {
                "database": str(request.database_path),
                "install": str(request.install_dir),
                "product": request.expected_product,
                "source_sha": request.expected_source_sha,
                "version": request.expected_version,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(payload).hexdigest()[:32]

    def _reject_other_active(self, operation_id: str, install_dir: Path) -> None:
        prefix = self._journal_prefix(install_dir)
        for path in self._state_dir.glob(f"{prefix}-*.json"):
            journal = self._read_journal(path)
            if journal.operation_id != operation_id and journal.phase not in _TERMINAL_PHASES:
                raise ActiveUpdateError(
                    f"another non-terminal update exists: {journal.operation_id}"
                )

    def _advance(self, journal: _Journal, phase: UpdatePhase) -> _Journal:
        return self._write_journal(
            replace(
                journal,
                phase=phase.value,
                updated_at=datetime.now(UTC).isoformat(),
            )
        )

    def _block(self, journal: _Journal, reason: str) -> _Journal:
        journal = self._write_journal(
            replace(
                journal,
                phase=UpdatePhase.BLOCKED.value,
                failure_reason=reason,
                updated_at=datetime.now(UTC).isoformat(),
            )
        )
        self._after_phase(UpdatePhase.BLOCKED)
        return journal

    def _write_journal(self, journal: _Journal) -> _Journal:
        path = self._journal_path(journal.operation_id, Path(journal.install_dir))
        content = json.dumps(
            asdict(journal), ensure_ascii=False, indent=2, sort_keys=True
        ) + "\n"
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self._state_dir,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
                temporary = Path(handle.name)
            os.replace(temporary, path)
            self._fsync_dir(path.parent)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return journal

    def _load_journal(self, operation_id: str, install_dir: Path) -> _Journal | None:
        path = self._journal_path(operation_id, install_dir)
        return self._read_journal(path) if path.exists() else None

    def _read_journal(self, path: Path) -> _Journal:
        try:
            payload = json.loads(
                path.read_text(encoding="utf-8-sig"),
                object_pairs_hook=self._unique_object,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, _DuplicateJsonKey) as exc:
            raise UpdateLifecycleError("durable update journal is malformed") from exc
        if not isinstance(payload, dict) or set(payload) != set(_Journal.__annotations__):
            raise UpdateLifecycleError("durable update journal schema is invalid")
        if payload.get("schema_version") != _JOURNAL_VERSION:
            raise UpdateLifecycleError("unsupported update journal version")
        try:
            journal = _Journal(**payload)
            UpdatePhase(journal.phase)
        except (TypeError, ValueError) as exc:
            raise UpdateLifecycleError("durable update journal values are invalid") from exc
        strings = (
            journal.operation_id,
            journal.phase,
            journal.install_dir,
            journal.database_path,
            journal.artifact_reference,
            journal.expected_product,
            journal.expected_version,
            journal.expected_source_sha,
            journal.installed_product,
            journal.installed_version,
            journal.installed_source_sha,
            journal.candidate_sha256,
            journal.created_at,
            journal.updated_at,
        )
        if not all(isinstance(value, str) and value for value in strings):
            raise UpdateLifecycleError("durable update journal contains invalid strings")
        if not _SOURCE_SHA_RE.fullmatch(journal.expected_source_sha):
            raise UpdateLifecycleError("journal target source SHA is invalid")
        if not _SOURCE_SHA_RE.fullmatch(journal.installed_source_sha):
            raise UpdateLifecycleError("journal installed source SHA is invalid")
        if not self._valid_sha256(journal.candidate_sha256):
            raise UpdateLifecycleError("journal candidate digest is invalid")
        if journal.restore_plan is not None and not isinstance(journal.restore_plan, dict):
            raise UpdateLifecycleError("journal restore plan is invalid")
        if journal.failure_reason is not None and not isinstance(journal.failure_reason, str):
            raise UpdateLifecycleError("journal failure reason is invalid")
        return journal

    def _extract_candidate(self, operation_id: str, artifact: Path, install_dir: Path) -> None:
        staged = self._staged(operation_id, install_dir)
        identity = self._read_archive_identity(artifact)
        if staged.exists():
            if not staged.is_dir() or self._read_installed_identity(staged) != identity:
                raise CandidateVerificationError("existing staged candidate has another identity")
            return
        temporary = staged.with_name(f"{staged.name}.extracting")
        if temporary.exists():
            shutil.rmtree(temporary)
        temporary.mkdir()
        try:
            with zipfile.ZipFile(artifact) as archive:
                archive.extractall(temporary)
            if self._read_installed_identity(temporary) != identity:
                raise CandidateVerificationError("extracted candidate identity changed")
            os.replace(temporary, staged)
            self._fsync_dir(staged.parent)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def _copy_private(self, source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.copying")
        try:
            shutil.copyfile(source, temporary)
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            self._fsync_dir(destination.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def _read_installed_identity(self, install_dir: Path) -> ReleaseIdentity:
        manifest = install_dir / "release-manifest.json"
        if not manifest.is_file():
            raise CandidateVerificationError(f"release manifest is missing: {manifest}")
        try:
            payload = json.loads(
                manifest.read_text(encoding="utf-8-sig"),
                object_pairs_hook=self._unique_object,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, _DuplicateJsonKey) as exc:
            raise CandidateVerificationError("installed release manifest is invalid") from exc
        return self._identity_from_payload(payload)

    def _read_archive_identity(self, artifact: Path) -> ReleaseIdentity:
        try:
            with zipfile.ZipFile(artifact) as archive:
                content = archive.read("release-manifest.json").decode("utf-8-sig")
            payload = json.loads(content, object_pairs_hook=self._unique_object)
        except (
            OSError,
            UnicodeError,
            KeyError,
            zipfile.BadZipFile,
            json.JSONDecodeError,
            _DuplicateJsonKey,
        ) as exc:
            raise CandidateVerificationError("candidate release manifest is invalid") from exc
        return self._identity_from_payload(payload)

    @staticmethod
    def _identity_from_payload(payload: object) -> ReleaseIdentity:
        if not isinstance(payload, dict) or frozenset(payload) != _MANIFEST_KEYS:
            raise CandidateVerificationError("release manifest schema is invalid")
        if payload.get("manifest_version") != _MANIFEST_VERSION:
            raise CandidateVerificationError("release manifest version is unsupported")
        product = payload.get("product")
        version = payload.get("version")
        source_sha = payload.get("source_sha")
        files = payload.get("files")
        if (
            not isinstance(product, str)
            or not product
            or product != product.strip()
            or not isinstance(version, str)
            or not version
            or version != version.strip()
            or not isinstance(source_sha, str)
            or not _SOURCE_SHA_RE.fullmatch(source_sha)
            or not isinstance(files, list)
            or not files
        ):
            raise CandidateVerificationError("release manifest identity is malformed")
        return ReleaseIdentity(product, version, source_sha)

    @staticmethod
    def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise _DuplicateJsonKey(key)
            result[key] = value
        return result

    @staticmethod
    def _new_identity_from_request(request: UpdateRequest) -> ReleaseIdentity:
        return ReleaseIdentity(
            request.expected_product,
            request.expected_version,
            request.expected_source_sha,
        )

    @staticmethod
    def _new_identity(journal: _Journal) -> ReleaseIdentity:
        return ReleaseIdentity(
            journal.expected_product,
            journal.expected_version,
            journal.expected_source_sha,
        )

    @staticmethod
    def _old_identity(journal: _Journal) -> ReleaseIdentity:
        return ReleaseIdentity(
            journal.installed_product,
            journal.installed_version,
            journal.installed_source_sha,
        )

    def _manager(self, database_path: Path) -> SQLiteRecoveryManager:
        return SQLiteRecoveryManager(SQLiteStore(database_path))

    def _operation_root(self, operation_id: str) -> Path:
        return self._state_dir / "operations" / operation_id

    def _private_artifact(self, operation_id: str) -> Path:
        return self._operation_root(operation_id) / "candidate.zip"

    def _private_evidence(self, operation_id: str) -> Path:
        return self._operation_root(operation_id) / "candidate-evidence.json"

    def _backup_path(self, operation_id: str) -> Path:
        return self._operation_root(operation_id) / f"pre-update-{operation_id}.sqlite3"

    @staticmethod
    def _backup_manifest(path: Path) -> Path:
        return path.with_name(f"{path.name}.manifest.json")

    @staticmethod
    def _staged(operation_id: str, install_dir: Path) -> Path:
        return install_dir.parent / f".{install_dir.name}.nika-update-{operation_id}.stage"

    @staticmethod
    def _rollback(operation_id: str, install_dir: Path) -> Path:
        return install_dir.parent / f".{install_dir.name}.nika-update-{operation_id}.rollback"

    @staticmethod
    def _failed(operation_id: str, install_dir: Path) -> Path:
        return install_dir.parent / f".{install_dir.name}.nika-update-{operation_id}.failed"

    def _journal_prefix(self, install_dir: Path) -> str:
        digest = hashlib.sha256(str(install_dir).encode()).hexdigest()[:16]
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
    def _fsync_dir(path: Path) -> None:
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
        """Deterministic process-loss seam used only by tests."""
        del phase
