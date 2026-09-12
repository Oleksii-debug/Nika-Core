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
        (
            "CREATE TABLE IF NOT EXISTS business_factory_snapshots ("
            "objective_id TEXT PRIMARY KEY, "
            "row_version INTEGER NOT NULL, "
            "payload_json TEXT NOT NULL, "
            "updated_at TEXT NOT NULL)"
        ),
    ),
}

_MIGRATION_COLUMNS = {
    "version": ("INTEGER", 0, 1),
    "applied_at": ("TEXT", 1, 0),
}
_SNAPSHOT_COLUMNS = {
    "objective_id": ("TEXT", 0, 1),
    "row_version": ("INTEGER", 1, 0),
    "payload_json": ("TEXT", 1, 0),
    "updated_at": ("TEXT", 1, 0),
}


def _require_stored_integer(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be stored as SQLite INTEGER")
    return value


def _table_schema(conn: Any, table_name: str) -> dict[str, tuple[str, int, int]]:
    if table_name == "business_factory_schema_migrations":
        rows = conn.execute(
            "PRAGMA table_info(business_factory_schema_migrations)"
        ).fetchall()
    elif table_name == "business_factory_snapshots":
        rows = conn.execute("PRAGMA table_info(business_factory_snapshots)").fetchall()
    else:
        raise ValueError("unsupported PF9 table name")
    return {
        str(row["name"]): (
            str(row["type"]).upper(),
            int(row["notnull"]),
            int(row["pk"]),
        )
        for row in rows
    }


def _validate_table(
    conn: Any,
    *,
    table_name: str,
    expected: dict[str, tuple[str, int, int]],
) -> None:
    actual = _table_schema(conn, table_name)
    if actual != expected:
        raise RuntimeError(f"business factory table schema mismatch: {table_name}")


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
            _validate_table(
                conn,
                table_name="business_factory_schema_migrations",
                expected=_MIGRATION_COLUMNS,
            )
            row = conn.execute(
                "SELECT MAX(version) AS version FROM business_factory_schema_migrations"
            ).fetchone()
            raw_current = row["version"] if row is not None else None
            current = (
                0
                if raw_current is None
                else _require_stored_integer(
                    raw_current,
                    field="business factory schema version",
                )
            )
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
            # A current marker is not proof that the owned table still exists or is intact.
            _validate_table(
                conn,
                table_name="business_factory_snapshots",
                expected=_SNAPSHOT_COLUMNS,
            )

    def save(
        self,
        snapshot: BusinessFactorySnapshot,
        *,
        expected_row_version: int,
    ) -> BusinessFactorySnapshot:
        if not isinstance(expected_row_version, int) or isinstance(expected_row_version, bool):
            raise StaleBusinessStateError("expected_row_version must be an integer")
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
            if expected_row_version == 0:
                inserted = conn.execute(
                    "INSERT INTO business_factory_snapshots("
                    "objective_id, row_version, payload_json, updated_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(objective_id) DO NOTHING",
                    (objective_id, snapshot.row_version, payload, now),
                )
                if inserted.rowcount != 1:
                    row = conn.execute(
                        "SELECT row_version FROM business_factory_snapshots "
                        "WHERE objective_id = ?",
                        (objective_id,),
                    ).fetchone()
                    if row is None:
                        current: str | int = "missing"
                    else:
                        current = _require_stored_integer(
                            row["row_version"],
                            field="business aggregate row version",
                        )
                    raise StaleBusinessStateError(
                        "business aggregate row version changed: "
                        f"{current} != {expected_row_version}"
                    )
            else:
                updated = conn.execute(
                    "UPDATE business_factory_snapshots SET row_version = ?, payload_json = ?, "
                    "updated_at = ? WHERE objective_id = ? AND row_version = ?",
                    (
                        snapshot.row_version,
                        payload,
                        now,
                        objective_id,
                        expected_row_version,
                    ),
                )
                if updated.rowcount != 1:
                    row = conn.execute(
                        "SELECT row_version FROM business_factory_snapshots "
                        "WHERE objective_id = ?",
                        (objective_id,),
                    ).fetchone()
                    if row is None:
                        raise StaleBusinessStateError(
                            "business aggregate does not exist at expected row version"
                        )
                    current = _require_stored_integer(
                        row["row_version"],
                        field="business aggregate row version",
                    )
                    raise StaleBusinessStateError(
                        "business aggregate row version changed: "
                        f"{current} != {expected_row_version}"
                    )
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
        stored_row_version = _require_stored_integer(
            row["row_version"],
            field="business aggregate row version",
        )
        snapshot = load_business_snapshot(str(row["payload_json"]))
        if snapshot.objective.objective_id != objective_id:
            raise RuntimeError("business snapshot objective identity does not match storage key")
        if snapshot.row_version != stored_row_version:
            raise RuntimeError("business snapshot row version does not match storage metadata")
        return snapshot
