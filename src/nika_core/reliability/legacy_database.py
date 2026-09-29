"""One-time legacy-location adoption through the canonical backup/restore service.

The old database remains in place. A receipt travels inside the restored database,
so process loss after restore cannot cause another adoption over newer user work.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from nika_core.data.multi_agent_state_schema import MULTI_AGENT_STATE_SCHEMA_VERSION
from nika_core.data.schema import SCHEMA_VERSION
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.model_artifact_schema import MODEL_ARTIFACT_SCHEMA_VERSION
from nika_core.product_project_schema import PRODUCT_PROJECT_SCHEMA_VERSION
from nika_core.reliability.backup import BackupRecoveryError, SQLiteRecoveryManager

_RECEIPT_TABLE = "legacy_database_adoption_v1"
_EMPTY_TABLES = {
    "schema_migrations",
    "multi_agent_state_schema_migrations",
    "model_artifact_schema_migrations",
    "product_project_schema_migrations",
    "sqlite_sequence",
    "audit_events",
    # FTS5 bookkeeping exists even with zero indexed documents. The actual
    # virtual table and its canonical content tables are still checked for rows.
    "corpus_fts_data",
    "corpus_fts_idx",
    "corpus_fts_docsize",
    "corpus_fts_config",
}
_MESSAGE = (
    "Потрібне відновлення даних Nika. Знайдено несумісні або різні бази даних. "
    "Автоматичне перенесення зупинено; старі дані збережено. "
    "Закрийте інші копії Nika та скористайтеся інструкцією відновлення даних."
)


class LegacyDatabaseConflict(BackupRecoveryError):
    """Startup cannot choose a database without a recovery decision."""


@dataclass(frozen=True, slots=True)
class _State:
    digest: str
    has_data: bool
    receipt: dict[str, str] | None


def default_legacy_locations() -> tuple[Path, ...]:
    return (
        Path.cwd() / "data" / "nika_core.db",
        Path(sys.executable).resolve().parent / "data" / "nika_core.db",
    )


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _require_empty_target(db: sqlite3.Connection) -> None:
    tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    if any(
        name not in _EMPTY_TABLES
        and db.execute(f"SELECT 1 FROM {_quoted(name)} LIMIT 1").fetchone() is not None
        for name in tables
    ):
        raise LegacyDatabaseConflict(_MESSAGE)


def _inspect(path: Path, *, canonical: bool = False) -> _State | None:
    if not path.exists():
        return None
    if not path.is_file() or path.is_symlink():
        raise LegacyDatabaseConflict(_MESSAGE)
    # rw permits SQLite's own hot-journal recovery on the canonical database;
    # it does not create a missing file. Legacy inspection is strictly read-only.
    mode = "rw" if canonical else "ro"
    with closing(sqlite3.connect(path.as_uri() + f"?mode={mode}", uri=True, timeout=2)) as db:
        db.execute("PRAGMA query_only = ON")
        db.execute("PRAGMA trusted_schema = OFF")
        db.execute("BEGIN")
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise LegacyDatabaseConflict(_MESSAGE)
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise LegacyDatabaseConflict(_MESSAGE)
        versions = [
            row[0] for row in db.execute("SELECT version FROM schema_migrations ORDER BY version")
        ]
        if not versions or any(type(v) is not int for v in versions):
            raise LegacyDatabaseConflict(_MESSAGE)
        if versions != list(range(1, versions[-1] + 1)) or versions[-1] > SCHEMA_VERSION:
            raise LegacyDatabaseConflict(_MESSAGE)
        tables = [
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        for name, supported in (
            ("multi_agent_state_schema_migrations", MULTI_AGENT_STATE_SCHEMA_VERSION),
            ("model_artifact_schema_migrations", MODEL_ARTIFACT_SCHEMA_VERSION),
            ("product_project_schema_migrations", PRODUCT_PROJECT_SCHEMA_VERSION),
        ):
            if name in tables:
                history = [
                    row[0]
                    for row in db.execute(f"SELECT version FROM {_quoted(name)} ORDER BY version")
                ]
                if any(type(v) is not int for v in history) or (
                    history
                    and (history != list(range(1, history[-1] + 1)) or history[-1] > supported)
                ):
                    raise LegacyDatabaseConflict(_MESSAGE)
        has_data = any(
            name not in _EMPTY_TABLES
            and db.execute(f"SELECT 1 FROM {_quoted(name)} LIMIT 1").fetchone() is not None
            for name in tables
        )
        digest = hashlib.sha256()
        for line in db.iterdump():
            digest.update(line.encode("utf-8"))
            digest.update(b"\n")
        receipt = None
        if _RECEIPT_TABLE in tables:
            rows = db.execute(
                f"SELECT adoption_id, source_path, source_digest FROM {_RECEIPT_TABLE}"
            ).fetchall()
            if len(rows) != 1:
                raise LegacyDatabaseConflict(_MESSAGE)
            receipt = dict(
                zip(("adoption_id", "source_path", "source_digest"), rows[0], strict=True)
            )
            _validate_receipt(receipt)
        return _State(digest.hexdigest(), has_data, receipt)


def _validate_receipt(receipt: dict[str, str]) -> None:
    for key, length in (("adoption_id", 32), ("source_digest", 64)):
        value = receipt[key]
        if (
            not isinstance(value, str)
            or len(value) != length
            or any(char not in "0123456789abcdef" for char in value)
        ):
            raise LegacyDatabaseConflict(_MESSAGE)
    source = receipt["source_path"]
    if (
        not isinstance(source, str)
        or not source
        or len(source) > 32768
        or not Path(source).is_absolute()
    ):
        raise LegacyDatabaseConflict(_MESSAGE)


def _source_unchanged(path: Path, digest: str) -> bool:
    state = _inspect(path)
    return state is not None and state.digest == digest


@contextmanager
def _startup_lock(target: Path) -> Iterator[None]:
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.with_name(f".{target.name}.startup.lock")
    if lock.is_symlink():
        raise LegacyDatabaseConflict(_MESSAGE)
    with lock.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise LegacyDatabaseConflict(_MESSAGE) from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _publish_pending(path: Path, record: dict[str, object]) -> None:
    temporary = path.with_name(path.name + f".{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def _known_sources(target: Path, candidates: Sequence[Path]) -> list[Path]:
    paths: list[Path] = []
    for candidate in candidates:
        if candidate.is_symlink():
            raise LegacyDatabaseConflict(_MESSAGE)
        path = candidate.resolve()
        if not path.exists() or path == target or (target.exists() and path.samefile(target)):
            continue
        if not any(path.samefile(other) for other in paths):
            paths.append(path)
    return paths


def prepare_default_database(target: Path, candidates: Sequence[Path]) -> None:
    """Adopt one valid legacy DB, or fail before the normal runtime can initialize.

    This is only for the default packaged location. Explicit configuration bypasses
    discovery entirely. No source removal, arbitrary disk search or merge of DBs.
    """
    if not target.is_absolute() or target.is_symlink():
        raise LegacyDatabaseConflict(_MESSAGE)
    target = target.resolve()
    try:
        with _startup_lock(target):
            _prepare_locked(target, candidates)
    except LegacyDatabaseConflict:
        raise
    except (OSError, sqlite3.DatabaseError, BackupRecoveryError, ValueError, KeyError, TypeError):
        raise LegacyDatabaseConflict(_MESSAGE) from None


def _prepare_locked(target: Path, candidates: Sequence[Path]) -> None:
    manager = SQLiteRecoveryManager(SQLiteStore(target))
    manager.recover_interrupted_restore()
    pending_path = target.with_name(f".{target.name}.legacy-adoption.json")
    pending = None
    if pending_path.exists():
        if pending_path.is_symlink() or pending_path.stat().st_size > 65536:
            raise LegacyDatabaseConflict(_MESSAGE)
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
        if (
            not isinstance(pending, dict)
            or set(pending)
            != {"version", "adoption_id", "source_path", "source_digest", "target_was_absent"}
            or type(pending["version"]) is not int
            or pending["version"] != 1
        ):
            raise LegacyDatabaseConflict(_MESSAGE)
        if type(pending["target_was_absent"]) is not bool:
            raise LegacyDatabaseConflict(_MESSAGE)
        _validate_receipt(pending)
    bootstrap_empty = bool(
        pending and pending["target_was_absent"] and target.is_file() and target.stat().st_size == 0
    )
    if bootstrap_empty:
        # A prior attempt reserved this absent target but did not commit backup.
        # Initialize its schema through the existing store, never quarantine or
        # replace unknown bytes. The guarded restore still rejects any new data.
        SQLiteStore(target).initialize()
    current = _inspect(target, canonical=True)
    receipt = current.receipt if current else None
    locations = list(candidates)
    if receipt or pending:
        locations.append(Path((receipt or pending)["source_path"]))
    sources = _known_sources(target, locations)
    states = {path: _inspect(path) for path in sources}
    if any(state is None for state in states.values()):
        raise LegacyDatabaseConflict(_MESSAGE)
    if receipt:
        if pending and any(receipt[key] != pending[key] for key in receipt):
            raise LegacyDatabaseConflict(_MESSAGE)
        original = Path(receipt["source_path"])
        for path, state in states.items():
            same_source = path == original or (original.exists() and path.samefile(original))
            if not same_source or state.digest != receipt["source_digest"]:
                raise LegacyDatabaseConflict(_MESSAGE)
        pending_path.unlink(missing_ok=True)
        return
    if not sources and not pending:
        return
    if len(sources) != 1 or (current and current.has_data):
        raise LegacyDatabaseConflict(_MESSAGE)
    source = sources[0]
    state = states[source]
    if state.receipt is not None:
        raise LegacyDatabaseConflict(_MESSAGE)
    if pending:
        if str(source) != pending["source_path"] or state.digest != pending["source_digest"]:
            raise LegacyDatabaseConflict(_MESSAGE)
        adoption_id = pending["adoption_id"]
    else:
        adoption_id = uuid4().hex
        backup_root = target.parent / "legacy-adoption-backups"
        backup_root.mkdir(exist_ok=True)
        with TemporaryDirectory(prefix="prepare-", dir=backup_root) as folder:
            baseline = backup_root / f"{adoption_id}.original.sqlite3"
            SQLiteRecoveryManager(SQLiteStore(source)).create_backup(baseline, record_audit=False)
            if not _source_unchanged(baseline, state.digest) or not _source_unchanged(
                source, state.digest
            ):
                raise LegacyDatabaseConflict(_MESSAGE)
            staged = SQLiteStore(Path(folder) / "prepared.sqlite3")
            stage_manager = SQLiteRecoveryManager(staged)
            plan = stage_manager.prepare_restore(baseline)
            stage_manager.restore(plan, confirmation_fingerprint=plan.confirmation_fingerprint)
            with staged.connection() as db:
                db.execute(
                    f"CREATE TABLE {_RECEIPT_TABLE} ("
                    "singleton INTEGER PRIMARY KEY CHECK(singleton = 1), "
                    "adoption_id TEXT NOT NULL, source_path TEXT NOT NULL, "
                    "source_digest TEXT NOT NULL)"
                )
                db.execute(
                    f"INSERT INTO {_RECEIPT_TABLE} VALUES (1, ?, ?, ?)",
                    (adoption_id, str(source), state.digest),
                )
            AuditLog(staged).append(
                event_type="reliability.legacy_database_adopted",
                entity_type="database",
                entity_id=adoption_id,
                payload={"source_digest": state.digest, "schema_version": 1},
            )
            stage_manager.create_backup(backup_root / f"{adoption_id}.sqlite3")
        pending = {
            "version": 1,
            "adoption_id": adoption_id,
            "source_path": str(source),
            "source_digest": state.digest,
            "target_was_absent": current is None,
        }
        _publish_pending(pending_path, pending)
    backup = target.parent / "legacy-adoption-backups" / f"{adoption_id}.sqlite3"
    manager.verify_backup(backup)
    backup_receipt = _inspect(backup).receipt
    if not backup_receipt or any(backup_receipt[key] != pending[key] for key in backup_receipt):
        raise LegacyDatabaseConflict(_MESSAGE)
    if not _source_unchanged(source, state.digest):
        raise LegacyDatabaseConflict(_MESSAGE)
    # Recheck the target immediately before the existing restore preview. No
    # task/team/permission/scheduler rows may be overwritten by automatic adoption.
    latest = _inspect(target, canonical=True)
    if latest and latest.has_data:
        raise LegacyDatabaseConflict(_MESSAGE)
    plan = manager.prepare_restore(backup)
    manager.restore(
        plan,
        confirmation_fingerprint=plan.confirmation_fingerprint,
        target_guard=_require_empty_target,
    )
    result = _inspect(target, canonical=True)
    if result is None or result.receipt != backup_receipt:
        raise LegacyDatabaseConflict(_MESSAGE)
    if not _source_unchanged(source, state.digest):
        raise LegacyDatabaseConflict(_MESSAGE)
    pending_path.unlink(missing_ok=True)
