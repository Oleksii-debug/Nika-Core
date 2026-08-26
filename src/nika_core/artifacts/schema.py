from __future__ import annotations

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


def initialize_artifact_registry_schema(store: SQLiteStore) -> None:
    """Apply Artifact Registry-owned ordered migrations in the canonical SQLite database."""
    with store.connection() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS artifact_registry_schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )"""
        )
        row = conn.execute(
            "SELECT MAX(version) AS version FROM artifact_registry_schema_migrations"
        ).fetchone()
        current = int(row["version"] or 0)
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
                "INSERT INTO artifact_registry_schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, datetime.now(UTC).isoformat()),
            )
