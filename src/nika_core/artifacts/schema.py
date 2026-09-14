from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from nika_core.data.sqlite import SQLiteStore

ARTIFACT_REGISTRY_SCHEMA_VERSION = 1

_ARTIFACT_REGISTRY_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """CREATE TABLE IF NOT EXISTS artifact_registry_records (
            artifact_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            kind TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
            location_kind TEXT NOT NULL CHECK(location_kind IN ('local_file','opaque_reference')),
            producer_id TEXT,
            record_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(workspace_id, idempotency_key)
        )""",
        """CREATE INDEX IF NOT EXISTS idx_artifact_registry_workspace_kind
        ON artifact_registry_records(workspace_id, kind, created_at, artifact_id)""",
        """CREATE INDEX IF NOT EXISTS idx_artifact_registry_sha256
        ON artifact_registry_records(sha256, artifact_id)""",
        """CREATE INDEX IF NOT EXISTS idx_artifact_registry_workspace_producer
        ON artifact_registry_records(workspace_id, producer_id, created_at, artifact_id)""",
        """CREATE TABLE IF NOT EXISTS artifact_registry_verifications (
            verification_id TEXT PRIMARY KEY,
            artifact_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('verified','missing','mismatch','unavailable')),
            verification_json TEXT NOT NULL,
            checked_at TEXT NOT NULL,
            FOREIGN KEY(artifact_id) REFERENCES artifact_registry_records(artifact_id)
        )""",
        """CREATE INDEX IF NOT EXISTS idx_artifact_registry_verifications
        ON artifact_registry_verifications(artifact_id, checked_at, verification_id)""",
    ),
}

_MIGRATION_COLUMNS = {
    "version": ("INTEGER", 0, 1),
    "applied_at": ("TEXT", 1, 0),
}
_RECORD_COLUMNS = {
    "artifact_id": ("TEXT", 0, 1),
    "workspace_id": ("TEXT", 1, 0),
    "idempotency_key": ("TEXT", 1, 0),
    "kind": ("TEXT", 1, 0),
    "sha256": ("TEXT", 1, 0),
    "size_bytes": ("INTEGER", 1, 0),
    "location_kind": ("TEXT", 1, 0),
    "producer_id": ("TEXT", 0, 0),
    "record_json": ("TEXT", 1, 0),
    "created_at": ("TEXT", 1, 0),
}
_VERIFICATION_COLUMNS = {
    "verification_id": ("TEXT", 0, 1),
    "artifact_id": ("TEXT", 1, 0),
    "state": ("TEXT", 1, 0),
    "verification_json": ("TEXT", 1, 0),
    "checked_at": ("TEXT", 1, 0),
}


def _stored_schema_version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("artifact registry schema version must be stored as SQLite INTEGER")
    return value


def _table_columns(
    conn: sqlite3.Connection,
    table_name: str,
) -> dict[str, tuple[str, int, int]]:
    if table_name == "artifact_registry_schema_migrations":
        rows = conn.execute(
            "PRAGMA table_info(artifact_registry_schema_migrations)"
        ).fetchall()
    elif table_name == "artifact_registry_records":
        rows = conn.execute("PRAGMA table_info(artifact_registry_records)").fetchall()
    elif table_name == "artifact_registry_verifications":
        rows = conn.execute(
            "PRAGMA table_info(artifact_registry_verifications)"
        ).fetchall()
    else:
        raise ValueError("unsupported Artifact Registry table")
    return {
        str(row["name"]): (
            str(row["type"]).upper(),
            int(row["notnull"]),
            int(row["pk"]),
        )
        for row in rows
    }


def _validate_table(
    conn: sqlite3.Connection,
    *,
    table_name: str,
    expected: dict[str, tuple[str, int, int]],
) -> None:
    if _table_columns(conn, table_name) != expected:
        raise RuntimeError(f"artifact registry table schema mismatch: {table_name}")


def _validate_owned_schema(conn: sqlite3.Connection) -> None:
    _validate_table(
        conn,
        table_name="artifact_registry_records",
        expected=_RECORD_COLUMNS,
    )
    _validate_table(
        conn,
        table_name="artifact_registry_verifications",
        expected=_VERIFICATION_COLUMNS,
    )


def initialize_artifact_registry_schema(store: SQLiteStore) -> None:
    """Apply Artifact Registry-owned ordered migrations in the canonical SQLite database."""
    with store.connection() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS artifact_registry_schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )"""
        )
        _validate_table(
            conn,
            table_name="artifact_registry_schema_migrations",
            expected=_MIGRATION_COLUMNS,
        )
        row = conn.execute(
            "SELECT MAX(version) AS version FROM artifact_registry_schema_migrations"
        ).fetchone()
        raw_current = row["version"] if row is not None else None
        current = 0 if raw_current is None else _stored_schema_version(raw_current)
        if current > ARTIFACT_REGISTRY_SCHEMA_VERSION:
            raise RuntimeError(
                "artifact registry schema "
                f"{current} is newer than supported schema {ARTIFACT_REGISTRY_SCHEMA_VERSION}"
            )
        for version in range(current + 1, ARTIFACT_REGISTRY_SCHEMA_VERSION + 1):
            statements = _ARTIFACT_REGISTRY_MIGRATIONS.get(version)
            if statements is None:
                raise RuntimeError(f"missing artifact registry migration {version}")
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO artifact_registry_schema_migrations(version, applied_at) "
                "VALUES (?, ?)",
                (version, datetime.now(UTC).isoformat()),
            )
        _validate_owned_schema(conn)
