from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from nika_core.business_factory import (
    BusinessFactorySnapshot,
    StaleBusinessStateError,
    dump_business_snapshot,
    load_business_snapshot,
)


BUSINESS_FACTORY_SCHEMA_VERSION = 1
BUSINESS_FACTORY_MIGRATIONS = {
    1: (
        "CREATE TABLE IF NOT EXISTS business_factory_snapshots ("
        "objective_id TEXT PRIMARY KEY, "
        "row_version INTEGER NOT NULL, "
        "payload_json TEXT NOT NULL, "
        "updated_at TEXT NOT NULL)",
    ),
}


class BusinessFactoryRepository:
    """PF9 durable aggregate store using Nika's canonical SQLiteStore connection boundary."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def initialize(self) -> None:
        with self.store.connection() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS business_factory_schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            row = conn.execute(
                "SELECT MAX(version) AS version FROM business_factory_schema_migrations"
            ).fetchone()
            current = int(row["version"] or 0)
            if current > BUSINESS_FACTORY_SCHEMA_VERSION:
                raise RuntimeError(
                    "business factory database schema "
                    f"{current} is newer than supported schema {BUSINESS_FACTORY_SCHEMA_VERSION}"
                )
            for version in range(current + 1, BUSINESS_FACTORY_SCHEMA_VERSION + 1):
                statements = BUSINESS_FACTORY_MIGRATIONS.get(version)
                if statements is None:
                    raise RuntimeError(f"missing business factory migration {version}")
                for statement in statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO business_factory_schema_migrations(version, applied_at) "
                    "VALUES (?, ?)",
                    (version, datetime.now(UTC).isoformat()),
                )

    def save(
        self,
        snapshot: BusinessFactorySnapshot,
        *,
        expected_row_version: int,
    ) -> BusinessFactorySnapshot:
        if expected_row_version < 0:
            raise StaleBusinessStateError("expected_row_version cannot be negative")
        if snapshot.row_version <= expected_row_version:
            raise StaleBusinessStateError(
                "business snapshot must advance beyond expected_row_version"
            )
        payload = dump_business_snapshot(snapshot)
        objective_id = snapshot.objective.objective_id
        now = datetime.now(UTC).isoformat()
        with self.store.connection() as conn:
            row = conn.execute(
                "SELECT row_version FROM business_factory_snapshots WHERE objective_id = ?",
                (objective_id,),
            ).fetchone()
            if row is None:
                if expected_row_version != 0:
                    raise StaleBusinessStateError(
                        "business aggregate does not exist at expected row version"
                    )
                conn.execute(
                    "INSERT INTO business_factory_snapshots("
                    "objective_id, row_version, payload_json, updated_at) VALUES (?, ?, ?, ?)",
                    (objective_id, snapshot.row_version, payload, now),
                )
            else:
                current = int(row["row_version"])
                if current != expected_row_version:
                    raise StaleBusinessStateError(
                        "business aggregate row version changed: "
                        f"{current} != {expected_row_version}"
                    )
                updated = conn.execute(
                    "UPDATE business_factory_snapshots SET row_version = ?, payload_json = ?, "
                    "updated_at = ? WHERE objective_id = ? AND row_version = ?",
                    (snapshot.row_version, payload, now, objective_id, expected_row_version),
                )
                if updated.rowcount != 1:
                    raise StaleBusinessStateError("business aggregate changed during save")
        return snapshot

    def load(self, objective_id: str) -> BusinessFactorySnapshot | None:
        if not isinstance(objective_id, str) or not objective_id.strip():
            raise ValueError("objective_id must be non-empty text")
        with self.store.connection() as conn:
            row = conn.execute(
                "SELECT row_version, payload_json FROM business_factory_snapshots "
                "WHERE objective_id = ?",
                (objective_id,),
            ).fetchone()
        if row is None:
            return None
        snapshot = load_business_snapshot(str(row["payload_json"]))
        if snapshot.objective.objective_id != objective_id:
            raise RuntimeError("business snapshot objective identity does not match storage key")
        if snapshot.row_version != int(row["row_version"]):
            raise RuntimeError("business snapshot row version does not match storage metadata")
        return snapshot
