"""Resource-only durability stream hosted by canonical SQLiteStore.

This is deliberately distinct from the historical mixed M3 v1/v2 stream: no
scheduler bindings or expanded resource-budget fields are claimed or migrated.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

RESOURCE_DURABILITY_SCHEMA_VERSION = 1
RESOURCE_DURABILITY_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """CREATE TABLE resource_requests (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            request_id TEXT NOT NULL,
            product_project_id TEXT,
            state TEXT NOT NULL CHECK(state IN (
                'waiting', 'granted', 'released', 'cancelled', 'released_after_restart'
            )),
            lease_owner_id TEXT,
            lease_owner_process_id INTEGER,
            lease_owner_started_at REAL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(scope, owner_id, request_id)
        )""",
        """CREATE INDEX idx_resource_requests_queue
        ON resource_requests(scope, owner_id, state, sequence)""",
        """CREATE INDEX idx_resource_requests_lease_owner
        ON resource_requests(lease_owner_id, state)""",
    ),
}
_MARKER = "resource_durability_schema_migrations"
_COLUMNS = {
    "sequence": ("INTEGER", 0, 1),
    "scope": ("TEXT", 1, 0),
    "owner_id": ("TEXT", 1, 0),
    "request_id": ("TEXT", 1, 0),
    "product_project_id": ("TEXT", 0, 0),
    "state": ("TEXT", 1, 0),
    "lease_owner_id": ("TEXT", 0, 0),
    "lease_owner_process_id": ("INTEGER", 0, 0),
    "lease_owner_started_at": ("REAL", 0, 0),
    "created_at": ("TEXT", 1, 0),
    "updated_at": ("TEXT", 1, 0),
}


def _exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def validate_resource_durability_schema(conn: sqlite3.Connection) -> int:
    """Read-only validation used by initialization and legacy-location inspection."""
    marker = _exists(conn, _MARKER)
    requests = _exists(conn, "resource_requests")
    if not marker:
        if requests:
            raise RuntimeError("resource durability table has no migration authority")
        return 0
    marker_columns = {
        row["name"]: (row["type"].upper(), row["notnull"], row["pk"])
        for row in conn.execute(f"PRAGMA table_info({_MARKER})")
    }
    if marker_columns != {"version": ("INTEGER", 0, 1), "applied_at": ("TEXT", 1, 0)}:
        raise RuntimeError("resource durability migration schema is corrupt")
    versions = [
        row["version"] for row in conn.execute(f"SELECT version FROM {_MARKER} ORDER BY version")
    ]
    if any(type(version) is not int for version in versions):
        raise RuntimeError("resource durability migration history is corrupt")
    if versions and versions[-1] > RESOURCE_DURABILITY_SCHEMA_VERSION:
        raise RuntimeError("resource durability database schema is newer than supported schema")
    if versions != list(range(1, len(versions) + 1)):
        raise RuntimeError("resource durability migration history is not contiguous")
    if not versions:
        if requests:
            raise RuntimeError("resource durability table has no migration authority")
        return 0
    if not _exists(conn, "resource_budgets") or not requests:
        raise RuntimeError("resource durability schema is missing required tables")
    columns = {
        row["name"]: (row["type"].upper(), row["notnull"], row["pk"])
        for row in conn.execute("PRAGMA table_info(resource_requests)")
    }
    if columns != _COLUMNS:
        raise RuntimeError("resource durability request schema is corrupt")
    indexes = conn.execute("PRAGMA index_list(resource_requests)").fetchall()
    shapes = {
        row["name"]: (
            row["unique"],
            row["partial"],
            tuple(
                part["name"]
                for part in conn.execute("SELECT name FROM pragma_index_info(?)", (row["name"],))
            ),
        )
        for row in indexes
    }
    if (
        shapes.get("idx_resource_requests_queue")
        != (0, 0, ("scope", "owner_id", "state", "sequence"))
        or shapes.get("idx_resource_requests_lease_owner")
        != (0, 0, ("lease_owner_id", "state"))
        or not any(
            shape == (1, 0, ("scope", "owner_id", "request_id"))
            for shape in shapes.values()
        )
    ):
        raise RuntimeError("resource durability request indexes are corrupt")
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'resource_requests'"
    ).fetchone()["sql"]
    normalized = "".join(sql.lower().split())
    if "sequenceintegerprimarykeyautoincrement" not in normalized:
        raise RuntimeError("resource durability FIFO sequence is corrupt")
    if (
        "check(statein('waiting','granted','released','cancelled','released_after_restart'))"
        not in normalized
    ):
        raise RuntimeError("resource durability state constraint is corrupt")
    return versions[-1]


def initialize_resource_durability_schema(conn: sqlite3.Connection) -> None:
    """Apply the additive resource-only stream on the canonical store connection."""
    current = validate_resource_durability_schema(conn)
    if not _exists(conn, "resource_budgets"):
        if _exists(conn, _MARKER):
            raise RuntimeError("resource durability schema is missing resource budgets")
        # Isolated core-migration fixtures can intentionally omit M3 prerequisites.
        return
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {_MARKER} ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for version in range(current + 1, RESOURCE_DURABILITY_SCHEMA_VERSION + 1):
        for statement in RESOURCE_DURABILITY_MIGRATIONS[version]:
            conn.execute(statement)
        conn.execute(
            f"INSERT INTO {_MARKER}(version, applied_at) VALUES (?, ?)",
            (version, datetime.now(UTC).isoformat()),
        )
    validate_resource_durability_schema(conn)
