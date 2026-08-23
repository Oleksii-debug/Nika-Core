from __future__ import annotations

import math
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from nika_core.data.sqlite import SQLiteStore
from nika_core.resources.contracts import (
    ResourceBudget,
    ResourceObserverPort,
    ResourceOwnerProbePort,
    ResourceProcessIdentity,
    ResourceRequestIdentity,
    ResourceSnapshot,
)


@dataclass(frozen=True, slots=True)
class ResourceDecision:
    granted: bool
    reason: str
    queue_position: int | None = None


class ResourceManager:
    def __init__(
        self,
        store: SQLiteStore,
        observer: ResourceObserverPort,
        *,
        manager_id: str | None = None,
        owner_probe: ResourceOwnerProbePort | None = None,
    ) -> None:
        self._store = store
        self._observer = observer
        self._manager_id = manager_id or uuid.uuid4().hex
        if not self._manager_id.strip():
            raise ValueError("manager_id must not be empty")
        if owner_probe is None and isinstance(observer, ResourceOwnerProbePort):
            owner_probe = observer
        self._owner_probe = owner_probe
        self._process_identity = (
            owner_probe.current_process_identity() if owner_probe is not None else None
        )
        self._owned_requests: set[tuple[str, str, str]] = set()
        self._lock = threading.RLock()

    @property
    def manager_id(self) -> str:
        return self._manager_id

    def set_budget(self, budget: ResourceBudget) -> None:
        _validate_budget(budget)
        with self._store.connection() as conn:
            conn.execute(
                """INSERT INTO resource_budgets(
                    scope, owner_id, max_concurrent, max_cpu_percent, max_memory_percent,
                    max_disk_percent, max_gpu_percent, max_process_memory_bytes, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope, owner_id) DO UPDATE SET
                    max_concurrent = excluded.max_concurrent,
                    max_cpu_percent = excluded.max_cpu_percent,
                    max_memory_percent = excluded.max_memory_percent,
                    max_disk_percent = excluded.max_disk_percent,
                    max_gpu_percent = excluded.max_gpu_percent,
                    max_process_memory_bytes = excluded.max_process_memory_bytes,
                    updated_at = excluded.updated_at
                """,
                (
                    budget.scope,
                    budget.owner_id,
                    budget.max_concurrent,
                    budget.max_cpu_percent,
                    budget.max_memory_percent,
                    budget.max_disk_percent,
                    budget.max_gpu_percent,
                    budget.max_process_memory_bytes,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def get_budget(self, *, scope: str, owner_id: str) -> ResourceBudget:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT * FROM resource_budgets WHERE scope = ? AND owner_id = ?",
                (scope, owner_id),
            ).fetchone()
        if row is None:
            return ResourceBudget(scope=scope, owner_id=owner_id)
        return ResourceBudget(
            scope=row["scope"],
            owner_id=row["owner_id"],
            max_concurrent=int(row["max_concurrent"]),
            max_cpu_percent=row["max_cpu_percent"],
            max_memory_percent=row["max_memory_percent"],
            max_disk_percent=row["max_disk_percent"],
            max_gpu_percent=row["max_gpu_percent"],
            max_process_memory_bytes=row["max_process_memory_bytes"],
        )

    def request(
        self,
        *,
        scope: str,
        owner_id: str,
        request_id: str,
        product_project_id: str | None = None,
    ) -> ResourceDecision:
        identity = ResourceRequestIdentity(
            scope=scope,
            owner_id=owner_id,
            request_id=request_id,
            product_project_id=product_project_id,
        )
        request_key = (identity.scope, identity.owner_id, identity.request_id)
        with self._lock, self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT * FROM resource_requests
                WHERE scope = ? AND owner_id = ? AND request_id = ?""",
                request_key,
            ).fetchone()
            if row is None:
                now = datetime.now(UTC).isoformat()
                conn.execute(
                    """INSERT INTO resource_requests(
                        scope, owner_id, request_id, product_project_id, state,
                        lease_owner_id, lease_owner_process_id, lease_owner_started_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'waiting', NULL, NULL, NULL, ?, ?)""",
                    (
                        identity.scope,
                        identity.owner_id,
                        identity.request_id,
                        identity.product_project_id,
                        now,
                        now,
                    ),
                )
            else:
                _validate_persisted_identity(row, identity)
                state = row["state"]
                if state == "granted":
                    if (
                        row["lease_owner_id"] == self._manager_id
                        and request_key in self._owned_requests
                    ):
                        return ResourceDecision(True, "already_granted")
                    return ResourceDecision(False, "recovery_required")
                if state in {"released", "cancelled", "released_after_restart"}:
                    created_at = row["created_at"]
                    conn.execute(
                        """DELETE FROM resource_requests
                        WHERE scope = ? AND owner_id = ? AND request_id = ?""",
                        request_key,
                    )
                    now = datetime.now(UTC).isoformat()
                    conn.execute(
                        """INSERT INTO resource_requests(
                            scope, owner_id, request_id, product_project_id, state,
                            lease_owner_id, lease_owner_process_id, lease_owner_started_at,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'waiting', NULL, NULL, NULL, ?, ?)""",
                        (
                            identity.scope,
                            identity.owner_id,
                            identity.request_id,
                            identity.product_project_id,
                            created_at,
                            now,
                        ),
                    )

            position = _queue_position(conn, identity)
            first = conn.execute(
                """SELECT request_id FROM resource_requests
                WHERE scope = ? AND owner_id = ? AND state = 'waiting'
                ORDER BY sequence LIMIT 1""",
                (identity.scope, identity.owner_id),
            ).fetchone()
            if first is None or first["request_id"] != identity.request_id:
                return ResourceDecision(False, "fifo_wait", position)

            if self._has_unowned_same_manager_lease(conn):
                return ResourceDecision(False, "recovery_required", position)

            budget = _get_budget_with_connection(conn, identity.scope, identity.owner_id)
            active = conn.execute(
                """SELECT COUNT(*) AS count FROM resource_requests
                WHERE scope = ? AND owner_id = ? AND state = 'granted'""",
                (identity.scope, identity.owner_id),
            ).fetchone()
            if int(active["count"]) >= budget.max_concurrent:
                return ResourceDecision(False, "concurrency_limit", position)

            snapshot = self._observer.snapshot()
            reason = _resource_pressure_reason(budget, snapshot)
            if reason is not None:
                return ResourceDecision(False, reason, position)

            process_id = (
                self._process_identity.process_id
                if self._process_identity is not None
                else None
            )
            started_at = (
                self._process_identity.started_at
                if self._process_identity is not None
                else None
            )
            cursor = conn.execute(
                """UPDATE resource_requests
                SET state = 'granted', lease_owner_id = ?,
                    lease_owner_process_id = ?, lease_owner_started_at = ?, updated_at = ?
                WHERE scope = ? AND owner_id = ? AND request_id = ? AND state = 'waiting'""",
                (
                    self._manager_id,
                    process_id,
                    started_at,
                    datetime.now(UTC).isoformat(),
                    identity.scope,
                    identity.owner_id,
                    identity.request_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("resource grant lost its durable waiting record")
            self._owned_requests.add(request_key)
            return ResourceDecision(True, "granted")

    def release(self, *, scope: str, owner_id: str, request_id: str) -> bool:
        ResourceRequestIdentity(scope=scope, owner_id=owner_id, request_id=request_id)
        request_key = (scope, owner_id, request_id)
        with self._lock:
            if request_key not in self._owned_requests:
                return False
            with self._store.connection() as conn:
                cursor = conn.execute(
                    """UPDATE resource_requests
                    SET state = 'released', lease_owner_id = NULL,
                        lease_owner_process_id = NULL, lease_owner_started_at = NULL,
                        updated_at = ?
                    WHERE scope = ? AND owner_id = ? AND request_id = ?
                      AND state = 'granted' AND lease_owner_id = ?""",
                    (
                        datetime.now(UTC).isoformat(),
                        scope,
                        owner_id,
                        request_id,
                        self._manager_id,
                    ),
                )
            if cursor.rowcount > 0:
                self._owned_requests.remove(request_key)
                return True
            return False

    def cancel_waiting(self, *, scope: str, owner_id: str, request_id: str) -> bool:
        ResourceRequestIdentity(scope=scope, owner_id=owner_id, request_id=request_id)
        with self._lock, self._store.connection() as conn:
            cursor = conn.execute(
                """UPDATE resource_requests
                SET state = 'cancelled', lease_owner_id = NULL,
                    lease_owner_process_id = NULL, lease_owner_started_at = NULL,
                    updated_at = ?
                WHERE scope = ? AND owner_id = ? AND request_id = ? AND state = 'waiting'""",
                (datetime.now(UTC).isoformat(), scope, owner_id, request_id),
            )
        return cursor.rowcount > 0

    def stale_lease_owners(self) -> tuple[str, ...]:
        """Return lease-owner recovery candidates; this does not prove they are stale."""
        with self._store.connection() as conn:
            rows = conn.execute(
                """SELECT DISTINCT lease_owner_id FROM resource_requests
                WHERE state = 'granted' AND lease_owner_id IS NOT NULL
                ORDER BY lease_owner_id"""
            ).fetchall()
        return tuple(row["lease_owner_id"] for row in rows)

    def recover_after_restart(self, *, stale_manager_id: str) -> int:
        """Release leases only after independent process-liveness proof says the owner is dead."""
        stale_manager_id = stale_manager_id.strip()
        if not stale_manager_id:
            raise ValueError("stale_manager_id must not be empty")
        if self._owner_probe is None:
            raise RuntimeError("resource owner liveness cannot be verified")
        with self._lock, self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """SELECT lease_owner_process_id, lease_owner_started_at
                FROM resource_requests
                WHERE state = 'granted' AND lease_owner_id = ?
                ORDER BY sequence""",
                (stale_manager_id,),
            ).fetchall()
            if not rows:
                return 0
            owner_identity = _persisted_process_identity(rows)
            if self._owner_probe.is_process_alive(owner_identity):
                raise RuntimeError("resource lease owner is still alive")
            cursor = conn.execute(
                """UPDATE resource_requests
                SET state = 'released_after_restart', lease_owner_id = NULL,
                    lease_owner_process_id = NULL, lease_owner_started_at = NULL,
                    updated_at = ?
                WHERE state = 'granted' AND lease_owner_id = ?
                  AND lease_owner_process_id = ? AND lease_owner_started_at = ?""",
                (
                    datetime.now(UTC).isoformat(),
                    stale_manager_id,
                    owner_identity.process_id,
                    owner_identity.started_at,
                ),
            )
            if cursor.rowcount != len(rows):
                raise RuntimeError("resource recovery owner identity changed during recovery")
        return int(cursor.rowcount)

    def active_count(self, *, scope: str, owner_id: str) -> int:
        with self._store.connection() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS count FROM resource_requests
                WHERE scope = ? AND owner_id = ? AND state = 'granted'""",
                (scope, owner_id),
            ).fetchone()
        return int(row["count"])

    def queued(self, *, scope: str, owner_id: str) -> tuple[str, ...]:
        with self._store.connection() as conn:
            rows = conn.execute(
                """SELECT request_id FROM resource_requests
                WHERE scope = ? AND owner_id = ? AND state = 'waiting'
                ORDER BY sequence""",
                (scope, owner_id),
            ).fetchall()
        return tuple(row["request_id"] for row in rows)

    def _has_unowned_same_manager_lease(self, conn: Any) -> bool:
        rows = conn.execute(
            """SELECT scope, owner_id, request_id FROM resource_requests
            WHERE state = 'granted' AND lease_owner_id = ?""",
            (self._manager_id,),
        ).fetchall()
        return any(
            (row["scope"], row["owner_id"], row["request_id"]) not in self._owned_requests
            for row in rows
        )


def _queue_position(conn: Any, identity: ResourceRequestIdentity) -> int:
    row = conn.execute(
        """SELECT COUNT(*) AS position
        FROM resource_requests
        WHERE scope = ? AND owner_id = ? AND state = 'waiting'
          AND sequence <= (
            SELECT sequence FROM resource_requests
            WHERE scope = ? AND owner_id = ? AND request_id = ?
          )""",
        (
            identity.scope,
            identity.owner_id,
            identity.scope,
            identity.owner_id,
            identity.request_id,
        ),
    ).fetchone()
    return int(row["position"])


def _persisted_process_identity(rows: list[Any]) -> ResourceProcessIdentity:
    identities: set[tuple[int, float]] = set()
    for row in rows:
        process_id = row["lease_owner_process_id"]
        started_at = row["lease_owner_started_at"]
        if process_id is None or started_at is None:
            raise RuntimeError("resource lease has no independently verifiable process identity")
        try:
            identity = ResourceProcessIdentity(
                process_id=process_id,
                started_at=started_at,
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError("resource lease process identity is corrupt") from exc
        identities.add((identity.process_id, identity.started_at))
    if len(identities) != 1:
        raise RuntimeError("resource manager ID spans multiple process identities")
    process_id, started_at = next(iter(identities))
    return ResourceProcessIdentity(process_id=process_id, started_at=started_at)


def _get_budget_with_connection(conn: Any, scope: str, owner_id: str) -> ResourceBudget:
    row = conn.execute(
        "SELECT * FROM resource_budgets WHERE scope = ? AND owner_id = ?",
        (scope, owner_id),
    ).fetchone()
    if row is None:
        return ResourceBudget(scope=scope, owner_id=owner_id)
    return ResourceBudget(
        scope=row["scope"],
        owner_id=row["owner_id"],
        max_concurrent=int(row["max_concurrent"]),
        max_cpu_percent=row["max_cpu_percent"],
        max_memory_percent=row["max_memory_percent"],
        max_disk_percent=row["max_disk_percent"],
        max_gpu_percent=row["max_gpu_percent"],
        max_process_memory_bytes=row["max_process_memory_bytes"],
    )


def _validate_persisted_identity(row: Any, identity: ResourceRequestIdentity) -> None:
    if row["product_project_id"] != identity.product_project_id:
        raise ValueError("resource request ProductProject identity cannot change")


def _resource_pressure_reason(
    budget: ResourceBudget,
    snapshot: ResourceSnapshot,
) -> str | None:
    if not _valid_snapshot(snapshot):
        return "invalid_observation"
    if budget.max_cpu_percent is not None and snapshot.cpu_percent > budget.max_cpu_percent:
        return "cpu_limit"
    if (
        budget.max_memory_percent is not None
        and snapshot.memory_percent > budget.max_memory_percent
    ):
        return "memory_limit"
    if budget.max_disk_percent is not None:
        if snapshot.disk_percent is None:
            return "disk_unavailable"
        if snapshot.disk_percent > budget.max_disk_percent:
            return "disk_limit"
    if budget.max_gpu_percent is not None:
        if snapshot.gpu_percent is None:
            return "gpu_unavailable"
        if snapshot.gpu_percent > budget.max_gpu_percent:
            return "gpu_limit"
    if budget.max_process_memory_bytes is not None:
        if snapshot.process_rss_bytes is None:
            return "process_memory_unavailable"
        if snapshot.process_rss_bytes > budget.max_process_memory_bytes:
            return "process_memory_limit"
    return None


def _valid_snapshot(snapshot: ResourceSnapshot) -> bool:
    percent_values = (
        snapshot.cpu_percent,
        snapshot.memory_percent,
        snapshot.disk_percent,
        snapshot.gpu_percent,
    )
    for value in percent_values:
        if value is not None and (not math.isfinite(value) or not 0 <= value <= 100):
            return False
    byte_values = (
        snapshot.available_memory_bytes,
        snapshot.available_disk_bytes,
        snapshot.process_rss_bytes,
    )
    return all(
        value is None
        or (isinstance(value, int) and not isinstance(value, bool) and value >= 0)
        for value in byte_values
    )


def _validate_budget(budget: ResourceBudget) -> None:
    if not budget.scope.strip() or not budget.owner_id.strip():
        raise ValueError("resource budget scope and owner_id must not be empty")
    if (
        isinstance(budget.max_concurrent, bool)
        or not isinstance(budget.max_concurrent, int)
        or budget.max_concurrent <= 0
    ):
        raise ValueError("max_concurrent must be a positive integer")
    for name, value in (
        ("max_cpu_percent", budget.max_cpu_percent),
        ("max_memory_percent", budget.max_memory_percent),
        ("max_disk_percent", budget.max_disk_percent),
        ("max_gpu_percent", budget.max_gpu_percent),
    ):
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0 < float(value) <= 100
        ):
            raise ValueError(f"{name} must be a finite number in the range (0, 100]")
    memory_bytes = budget.max_process_memory_bytes
    if memory_bytes is not None and (
        isinstance(memory_bytes, bool) or not isinstance(memory_bytes, int) or memory_bytes <= 0
    ):
        raise ValueError("max_process_memory_bytes must be a positive integer or None")
