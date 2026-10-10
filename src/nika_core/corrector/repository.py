from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Protocol

from nika_core.corrector.contracts import (
    CorrectionEvidence,
    CorrectionProfile,
    CorrectionResult,
    CorrectionSession,
    CorrectorConflict,
    CorrectorError,
    CorrectorIntegrityError,
    SessionRevision,
    _validate_identifier,
    sha256_text,
)
from nika_core.corrector.engine import correct_text


CORRECTOR_SCHEMA_VERSION = 1
INITIAL_OPERATION_PREFIX = "initial:"


class ConnectionStore(Protocol):
    def connection(self) -> AbstractContextManager[sqlite3.Connection]: ...


_SCHEMA_V1 = (
    """
    CREATE TABLE IF NOT EXISTS corrector_sessions (
        session_id TEXT PRIMARY KEY,
        current_revision INTEGER NOT NULL,
        text TEXT NOT NULL,
        text_digest TEXT NOT NULL,
        profile_json TEXT NOT NULL,
        profile_digest TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS corrector_revisions (
        session_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        operation_id TEXT NOT NULL,
        expected_revision INTEGER NOT NULL,
        text TEXT NOT NULL,
        text_digest TEXT NOT NULL,
        parent_digest TEXT,
        profile_json TEXT NOT NULL,
        profile_digest TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (session_id, revision),
        UNIQUE (session_id, operation_id),
        FOREIGN KEY (session_id) REFERENCES corrector_sessions(session_id) ON DELETE CASCADE
    )
    """,
    (
        "CREATE INDEX IF NOT EXISTS idx_corrector_revisions_session "
        "ON corrector_revisions(session_id, revision)"
    ),
)


def _db_int(value: object, field_name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise CorrectorIntegrityError(f"{field_name} is not a valid durable integer")
    return value


def _db_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise CorrectorIntegrityError(f"{field_name} is not valid durable text")
    return value


def _parse_utc(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CorrectorIntegrityError(f"{field_name} is not a valid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CorrectorIntegrityError(f"{field_name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_timestamp(value: datetime | None, field_name: str) -> datetime:
    candidate = value or datetime.now(UTC)
    if not isinstance(candidate, datetime):
        raise CorrectorError(f"{field_name} must be a datetime")
    if candidate.tzinfo is None or candidate.utcoffset() is None:
        raise CorrectorError(f"{field_name} must be timezone-aware")
    return candidate.astimezone(UTC)


class CorrectorRepository:
    """Durable local Corrector state using a Nika-compatible SQLite connection store."""

    def __init__(self, store: ConnectionStore) -> None:
        self._store = store

    def initialize(self) -> None:
        with self._store.connection() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS corrector_schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            row = conn.execute(
                "SELECT MAX(version) AS version FROM corrector_schema_migrations"
            ).fetchone()
            current = _db_int(row["version"] or 0, "corrector schema version")
            if current > CORRECTOR_SCHEMA_VERSION:
                raise CorrectorIntegrityError(
                    f"corrector schema {current} is newer than supported schema "
                    f"{CORRECTOR_SCHEMA_VERSION}"
                )
            if current < 1:
                for statement in _SCHEMA_V1:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO corrector_schema_migrations(version, applied_at) VALUES (?, ?)",
                    (1, _now_iso()),
                )
            self._verify_schema(conn)

    @staticmethod
    def _verify_schema(conn: sqlite3.Connection) -> None:
        expected = {
            "corrector_schema_migrations": ("version", "applied_at"),
            "corrector_sessions": (
                "session_id",
                "current_revision",
                "text",
                "text_digest",
                "profile_json",
                "profile_digest",
                "updated_at",
            ),
            "corrector_revisions": (
                "session_id",
                "revision",
                "operation_id",
                "expected_revision",
                "text",
                "text_digest",
                "parent_digest",
                "profile_json",
                "profile_digest",
                "evidence_json",
                "created_at",
            ),
        }
        for table, expected_columns in expected.items():
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
            columns = tuple(row["name"] for row in rows)
            if columns != expected_columns:
                raise CorrectorIntegrityError(
                    f"corrector schema table {table} has unexpected columns"
                )

    def create_session(
        self,
        session_id: str,
        text: str,
        profile: CorrectionProfile,
        *,
        created_at: datetime | None = None,
    ) -> CorrectionSession:
        session_id = _validate_identifier(session_id, "session_id")
        if not isinstance(text, str):
            raise CorrectorError("text must be a string")
        if not isinstance(profile, CorrectionProfile):
            raise CorrectorError("profile must be CorrectionProfile")
        created = _normalize_timestamp(created_at, "created_at").isoformat()
        operation_id = f"{INITIAL_OPERATION_PREFIX}{session_id}"
        input_digest = sha256_text(text)
        evidence = CorrectionEvidence(
            profile_digest=profile.digest,
            input_digest=input_digest,
            output_digest=input_digest,
            normalized_changed=False,
            protected_occurrences=0,
            rule_changes=(),
        )
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM corrector_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if existing is not None:
                session, history = self._validate_session_and_history(conn, session_id)
                initial = history[0]
                if initial.operation_id != operation_id:
                    raise CorrectorIntegrityError("initial session revision is missing or rebound")
                if initial.text_digest != input_digest or initial.profile.digest != profile.digest:
                    raise CorrectorConflict(
                        "session_id is already bound to different initial state"
                    )
                return session
            conn.execute(
                "INSERT INTO corrector_sessions("
                "session_id, current_revision, text, text_digest, profile_json, "
                "profile_digest, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    0,
                    text,
                    input_digest,
                    profile.canonical_json(),
                    profile.digest,
                    created,
                ),
            )
            conn.execute(
                "INSERT INTO corrector_revisions("
                "session_id, revision, operation_id, expected_revision, text, text_digest, "
                "parent_digest, profile_json, profile_digest, evidence_json, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    0,
                    operation_id,
                    0,
                    text,
                    input_digest,
                    None,
                    profile.canonical_json(),
                    profile.digest,
                    evidence.canonical_json(),
                    created,
                ),
            )
            return CorrectionSession(
                session_id=session_id,
                current_revision=0,
                text=text,
                text_digest=input_digest,
                profile=profile,
            )

    def apply(
        self,
        session_id: str,
        operation_id: str,
        profile: CorrectionProfile,
        *,
        expected_revision: int,
        created_at: datetime | None = None,
    ) -> SessionRevision:
        session_id = _validate_identifier(session_id, "session_id")
        operation_id = _validate_identifier(operation_id, "operation_id")
        if operation_id.startswith(INITIAL_OPERATION_PREFIX):
            raise CorrectorError("operation_id uses a reserved prefix")
        if not isinstance(profile, CorrectionProfile):
            raise CorrectorError("profile must be CorrectionProfile")
        if type(expected_revision) is not int or expected_revision < 0:
            raise CorrectorError("expected_revision must be a non-negative integer")
        created = _normalize_timestamp(created_at, "created_at")

        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            session, history = self._validate_session_and_history(conn, session_id)
            replay = conn.execute(
                "SELECT * FROM corrector_revisions WHERE session_id = ? AND operation_id = ?",
                (session_id, operation_id),
            ).fetchone()
            if replay is not None:
                durable_expected = _db_int(replay["expected_revision"], "expected_revision")
                if (
                    durable_expected != expected_revision
                    or replay["profile_digest"] != profile.digest
                ):
                    raise CorrectorConflict("operation_id replay has conflicting inputs")
                return self._row_to_revision(replay)
            if session.current_revision != expected_revision:
                raise CorrectorConflict(
                    f"expected revision {expected_revision}, current is {session.current_revision}"
                )
            previous = history[-1]
            previous_at = _parse_utc(previous.created_at, "created_at")
            if created < previous_at:
                raise CorrectorConflict("created_at cannot precede the previous revision")
            result = correct_text(session.text, profile)
            revision_number = session.current_revision + 1
            created_text = created.isoformat()
            conn.execute(
                "INSERT INTO corrector_revisions("
                "session_id, revision, operation_id, expected_revision, text, text_digest, "
                "parent_digest, profile_json, profile_digest, evidence_json, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    revision_number,
                    operation_id,
                    expected_revision,
                    result.after_text,
                    result.evidence.output_digest,
                    session.text_digest,
                    profile.canonical_json(),
                    profile.digest,
                    result.evidence.canonical_json(),
                    created_text,
                ),
            )
            conn.execute(
                "UPDATE corrector_sessions SET current_revision = ?, text = ?, text_digest = ?, "
                "profile_json = ?, profile_digest = ?, updated_at = ? WHERE session_id = ?",
                (
                    revision_number,
                    result.after_text,
                    result.evidence.output_digest,
                    profile.canonical_json(),
                    profile.digest,
                    created_text,
                    session_id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM corrector_revisions WHERE session_id = ? AND revision = ?",
                (session_id, revision_number),
            ).fetchone()
            if row is None:
                raise CorrectorIntegrityError("committed revision could not be materialized")
            return self._row_to_revision(row)

    def get_session(self, session_id: str) -> CorrectionSession:
        session_id = _validate_identifier(session_id, "session_id")
        with self._store.connection() as conn:
            return self._validate_session_and_history(conn, session_id)[0]

    def history(self, session_id: str) -> tuple[SessionRevision, ...]:
        session_id = _validate_identifier(session_id, "session_id")
        with self._store.connection() as conn:
            return self._validate_session_and_history(conn, session_id)[1]

    def _validate_session_and_history(
        self,
        conn: sqlite3.Connection,
        session_id: str,
    ) -> tuple[CorrectionSession, tuple[SessionRevision, ...]]:
        row = conn.execute(
            "SELECT * FROM corrector_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise CorrectorError(f"unknown corrector session: {session_id}")
        current_revision = _db_int(row["current_revision"], "current_revision")
        text = row["text"]
        if not isinstance(text, str):
            raise CorrectorIntegrityError("session text is not valid durable text")
        text_digest = _db_text(row["text_digest"], "text_digest")
        profile_json = _db_text(row["profile_json"], "profile_json")
        profile = CorrectionProfile.from_canonical_json(profile_json)
        if row["profile_digest"] != profile.digest:
            raise CorrectorIntegrityError("session profile digest mismatch")
        updated_at = _parse_utc(
            _db_text(row["updated_at"], "updated_at"), "updated_at"
        )
        session = CorrectionSession(
            session_id=session_id,
            current_revision=current_revision,
            text=text,
            text_digest=text_digest,
            profile=profile,
        )

        rows = conn.execute(
            "SELECT * FROM corrector_revisions WHERE session_id = ? ORDER BY revision",
            (session_id,),
        ).fetchall()
        if len(rows) != current_revision + 1:
            raise CorrectorIntegrityError("revision history length does not match current revision")
        history: list[SessionRevision] = []
        seen_operations: set[str] = set()
        previous_digest: str | None = None
        previous_at: datetime | None = None
        for index, revision_row in enumerate(rows):
            revision = self._row_to_revision(revision_row)
            if revision.revision != index:
                raise CorrectorIntegrityError("revision history is not contiguous")
            if revision.operation_id in seen_operations:
                raise CorrectorIntegrityError("operation_id is duplicated in revision history")
            seen_operations.add(revision.operation_id)
            if index == 0:
                if revision.parent_digest is not None:
                    raise CorrectorIntegrityError("initial revision must not have a parent digest")
                if revision.operation_id != f"{INITIAL_OPERATION_PREFIX}{session_id}":
                    raise CorrectorIntegrityError("initial revision operation identity is invalid")
                if _db_int(revision_row["expected_revision"], "expected_revision") != 0:
                    raise CorrectorIntegrityError("initial expected_revision must be zero")
                if revision.evidence.input_digest != revision.text_digest:
                    raise CorrectorIntegrityError("initial evidence input must equal initial text")
                if (
                    revision.evidence.normalized_changed
                    or revision.evidence.protected_occurrences != 0
                    or revision.evidence.total_replacements != 0
                ):
                    raise CorrectorIntegrityError("initial evidence must be an identity record")
            else:
                if revision.parent_digest != previous_digest:
                    raise CorrectorIntegrityError("revision parent digest does not match lineage")
                expected = _db_int(revision_row["expected_revision"], "expected_revision")
                if expected != index - 1:
                    raise CorrectorIntegrityError(
                        "revision expected_revision does not match lineage"
                    )
                if revision.evidence.input_digest != previous_digest:
                    raise CorrectorIntegrityError("revision evidence input does not match parent")
            timestamp = _parse_utc(revision.created_at, "created_at")
            if previous_at is not None and timestamp < previous_at:
                raise CorrectorIntegrityError("revision timestamps rewind")
            previous_at = timestamp
            previous_digest = revision.text_digest
            history.append(revision)
        head = history[-1]
        if (
            head.revision != session.current_revision
            or head.text_digest != session.text_digest
            or head.text != session.text
            or head.profile.digest != session.profile.digest
        ):
            raise CorrectorIntegrityError("session head does not match revision authority")
        if updated_at != _parse_utc(head.created_at, "head created_at"):
            raise CorrectorIntegrityError("session updated_at does not match revision head")
        return session, tuple(history)

    @staticmethod
    def _row_to_revision(row: sqlite3.Row) -> SessionRevision:
        revision = _db_int(row["revision"], "revision")
        text = row["text"]
        if not isinstance(text, str):
            raise CorrectorIntegrityError("revision text is not valid durable text")
        profile_json = _db_text(row["profile_json"], "profile_json")
        profile = CorrectionProfile.from_canonical_json(profile_json)
        profile_digest = _db_text(row["profile_digest"], "profile_digest")
        if profile_digest != profile.digest:
            raise CorrectorIntegrityError("revision profile digest mismatch")
        evidence_json = _db_text(row["evidence_json"], "evidence_json")
        evidence = CorrectionEvidence.from_canonical_json(evidence_json)
        parent_digest = row["parent_digest"]
        if parent_digest is not None and not isinstance(parent_digest, str):
            raise CorrectorIntegrityError("parent_digest is not durable text")
        return SessionRevision(
            session_id=_db_text(row["session_id"], "session_id"),
            revision=revision,
            text=text,
            text_digest=_db_text(row["text_digest"], "text_digest"),
            parent_digest=parent_digest,
            profile=profile,
            evidence=evidence,
            operation_id=_db_text(row["operation_id"], "operation_id"),
            created_at=_db_text(row["created_at"], "created_at"),
        )


def revision_result(previous: SessionRevision, current: SessionRevision) -> CorrectionResult:
    if current.revision != previous.revision + 1 or current.parent_digest != previous.text_digest:
        raise CorrectorIntegrityError("revisions are not a direct parent-child pair")
    return CorrectionResult(
        before_text=previous.text,
        after_text=current.text,
        evidence=current.evidence,
    )
