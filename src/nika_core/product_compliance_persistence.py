from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from nika_core.product_compliance import (
    ProductComplianceError,
    ProductComplianceSnapshot,
    dump_compliance_snapshot,
    load_compliance_snapshot,
)

PRODUCT_COMPLIANCE_SCHEMA_VERSION = 1
PRODUCT_COMPLIANCE_MIGRATIONS = {
    1: (
        (
            "CREATE TABLE IF NOT EXISTS product_compliance_snapshots ("
            "project_id TEXT PRIMARY KEY, "
            "revision INTEGER NOT NULL, "
            "snapshot_digest TEXT NOT NULL, "
            "payload_json TEXT NOT NULL, "
            "updated_at TEXT NOT NULL)"
        ),
    ),
}


class StaleProductComplianceStateError(ProductComplianceError):
    pass


class ProductComplianceRepository:
    """Durable PF10 current-state authority over Nika's canonical SQLiteStore."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def initialize(self) -> None:
        with self.store.connection() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS product_compliance_schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            row = conn.execute(
                "SELECT MAX(version) AS version FROM product_compliance_schema_migrations"
            ).fetchone()
            current = int(row["version"] or 0)
            if current > PRODUCT_COMPLIANCE_SCHEMA_VERSION:
                raise RuntimeError(
                    "product compliance database schema "
                    f"{current} is newer than supported schema "
                    f"{PRODUCT_COMPLIANCE_SCHEMA_VERSION}"
                )
            for version in range(current + 1, PRODUCT_COMPLIANCE_SCHEMA_VERSION + 1):
                statements = PRODUCT_COMPLIANCE_MIGRATIONS.get(version)
                if statements is None:
                    raise RuntimeError(f"missing product compliance migration {version}")
                for statement in statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO product_compliance_schema_migrations(version, applied_at) "
                    "VALUES (?, ?)",
                    (version, datetime.now(UTC).isoformat()),
                )

    def save(
        self,
        snapshot: ProductComplianceSnapshot,
        *,
        expected_revision: int,
    ) -> ProductComplianceSnapshot:
        if not isinstance(snapshot, ProductComplianceSnapshot):
            raise ProductComplianceError(
                "compliance repository requires ProductComplianceSnapshot"
            )
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise StaleProductComplianceStateError(
                "expected compliance revision must be a non-negative int"
            )
        if snapshot.revision != expected_revision + 1:
            raise StaleProductComplianceStateError(
                "compliance snapshot must advance exactly one revision"
            )
        payload = dump_compliance_snapshot(snapshot)
        now = datetime.now(UTC).isoformat()
        with self.store.connection() as conn:
            row = conn.execute(
                "SELECT revision FROM product_compliance_snapshots WHERE project_id = ?",
                (snapshot.project_id,),
            ).fetchone()
            if row is None:
                if expected_revision != 0:
                    raise StaleProductComplianceStateError(
                        "compliance state does not exist at expected revision"
                    )
                conn.execute(
                    "INSERT INTO product_compliance_snapshots("
                    "project_id, revision, snapshot_digest, payload_json, updated_at"
                    ") VALUES (?, ?, ?, ?, ?)",
                    (
                        snapshot.project_id,
                        snapshot.revision,
                        snapshot.digest,
                        payload,
                        now,
                    ),
                )
            else:
                current = int(row["revision"])
                if current != expected_revision:
                    raise StaleProductComplianceStateError(
                        f"compliance revision changed: {current} != {expected_revision}"
                    )
                updated = conn.execute(
                    "UPDATE product_compliance_snapshots SET revision = ?, "
                    "snapshot_digest = ?, payload_json = ?, updated_at = ? "
                    "WHERE project_id = ? AND revision = ?",
                    (
                        snapshot.revision,
                        snapshot.digest,
                        payload,
                        now,
                        snapshot.project_id,
                        expected_revision,
                    ),
                )
                if updated.rowcount != 1:
                    raise StaleProductComplianceStateError(
                        "compliance state changed during save"
                    )
        return snapshot

    def current_snapshot(self, *, project_id: str) -> ProductComplianceSnapshot | None:
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError("project_id must be non-empty text")
        with self.store.connection() as conn:
            row = conn.execute(
                "SELECT revision, snapshot_digest, payload_json "
                "FROM product_compliance_snapshots WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            return None
        snapshot = load_compliance_snapshot(str(row["payload_json"]))
        if snapshot.project_id != project_id:
            raise RuntimeError(
                "compliance snapshot project identity does not match storage key"
            )
        if snapshot.revision != int(row["revision"]):
            raise RuntimeError(
                "compliance snapshot revision does not match storage metadata"
            )
        if snapshot.digest != str(row["snapshot_digest"]):
            raise RuntimeError(
                "compliance snapshot digest does not match storage metadata"
            )
        return snapshot
