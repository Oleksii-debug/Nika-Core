from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
import math

from nika_core.data.sqlite import SQLiteStore
from nika_core.resources.contracts import (
    ResourceBudget,
    ResourceCapacityStatus,
    ResourceObserverPort,
    ResourceSnapshot,
)

_SQLITE_MAX_INT64 = (1 << 63) - 1


class ResourceTelemetryError(RuntimeError):
    """Raised when host resource telemetry cannot be trusted."""


@dataclass(frozen=True, slots=True)
class ResourceDecision:
    granted: bool
    reason: str
    queue_position: int | None = None


class ResourceManager:
    def __init__(self, store: SQLiteStore, observer: ResourceObserverPort) -> None:
        self._store = store
        self._observer = observer
        self._active: dict[tuple[str, str], set[str]] = {}
        self._queues: dict[tuple[str, str], deque[str]] = {}

    def set_budget(self, budget: ResourceBudget) -> None:
        _validate_budget(budget)
        with self._store.connection() as conn:
            conn.execute(
                """INSERT INTO resource_budgets(
                    scope, owner_id, max_concurrent, max_cpu_percent, max_memory_percent, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope, owner_id) DO UPDATE SET
                    max_concurrent = excluded.max_concurrent,
                    max_cpu_percent = excluded.max_cpu_percent,
                    max_memory_percent = excluded.max_memory_percent,
                    updated_at = excluded.updated_at
                """,
                (
                    budget.scope,
                    budget.owner_id,
                    budget.max_concurrent,
                    budget.max_cpu_percent,
                    budget.max_memory_percent,
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
        )

    def status(self, *, scope: str, owner_id: str) -> ResourceCapacityStatus:
        """Return deterministic read-only capacity telemetry without changing admission state."""
        budget = self.get_budget(scope=scope, owner_id=owner_id)
        snapshot = self._observer.snapshot()
        if not _valid_snapshot(snapshot):
            raise ResourceTelemetryError("resource telemetry invalid")
        active_count = self.active_count(scope=scope, owner_id=owner_id)
        queued_count = len(self.queued(scope=scope, owner_id=owner_id))
        pressure_reasons: list[str] = []

        if active_count >= budget.max_concurrent:
            pressure_reasons.append("concurrency_limit")
        if budget.max_cpu_percent is not None and snapshot.cpu_percent > budget.max_cpu_percent:
            pressure_reasons.append("cpu_limit")
        if (
            budget.max_memory_percent is not None
            and snapshot.memory_percent > budget.max_memory_percent
        ):
            pressure_reasons.append("memory_limit")

        return ResourceCapacityStatus(
            budget=budget,
            snapshot=snapshot,
            active_count=active_count,
            queued_count=queued_count,
            concurrency_headroom=max(0, budget.max_concurrent - active_count),
            cpu_headroom_percent=(
                None
                if budget.max_cpu_percent is None
                else budget.max_cpu_percent - snapshot.cpu_percent
            ),
            memory_headroom_percent=(
                None
                if budget.max_memory_percent is None
                else budget.max_memory_percent - snapshot.memory_percent
            ),
            pressure_reasons=tuple(pressure_reasons),
        )

    def request(self, *, scope: str, owner_id: str, request_id: str) -> ResourceDecision:
        if not request_id.strip():
            raise ValueError("request_id must not be empty")
        key = (scope, owner_id)
        active = self._active.setdefault(key, set())
        queue = self._queues.setdefault(key, deque())
        if request_id in active:
            return ResourceDecision(True, "already_granted")
        if request_id not in queue:
            queue.append(request_id)
        position = queue.index(request_id) + 1
        if queue[0] != request_id:
            return ResourceDecision(False, "fifo_wait", position)

        budget = self.get_budget(scope=scope, owner_id=owner_id)
        if len(active) >= budget.max_concurrent:
            return ResourceDecision(False, "concurrency_limit", position)
        snapshot = self._observer.snapshot()
        if not _valid_snapshot(snapshot):
            return ResourceDecision(False, "invalid_observation", position)
        if budget.max_cpu_percent is not None and snapshot.cpu_percent > budget.max_cpu_percent:
            return ResourceDecision(False, "cpu_limit", position)
        if (
            budget.max_memory_percent is not None
            and snapshot.memory_percent > budget.max_memory_percent
        ):
            return ResourceDecision(False, "memory_limit", position)

        queue.popleft()
        active.add(request_id)
        return ResourceDecision(True, "granted")

    def revalidate(self, *, scope: str, owner_id: str, request_id: str) -> ResourceDecision:
        """Recheck a live grant against current resource limits without changing queue state."""
        if not request_id.strip():
            raise ValueError("request_id must not be empty")
        key = (scope, owner_id)
        active = self._active.setdefault(key, set())
        if request_id not in active:
            return ResourceDecision(False, "not_granted")

        budget = self.get_budget(scope=scope, owner_id=owner_id)
        if len(active) > budget.max_concurrent:
            return ResourceDecision(False, "concurrency_limit")

        snapshot = self._observer.snapshot()
        if not _valid_snapshot(snapshot):
            return ResourceDecision(False, "invalid_observation")
        if budget.max_cpu_percent is not None and snapshot.cpu_percent > budget.max_cpu_percent:
            return ResourceDecision(False, "cpu_limit")
        if (
            budget.max_memory_percent is not None
            and snapshot.memory_percent > budget.max_memory_percent
        ):
            return ResourceDecision(False, "memory_limit")
        return ResourceDecision(True, "still_granted")

    def release(self, *, scope: str, owner_id: str, request_id: str) -> bool:
        key = (scope, owner_id)
        active = self._active.setdefault(key, set())
        if request_id not in active:
            return False
        active.remove(request_id)
        return True

    def cancel_waiting(self, *, scope: str, owner_id: str, request_id: str) -> bool:
        queue = self._queues.setdefault((scope, owner_id), deque())
        try:
            queue.remove(request_id)
        except ValueError:
            return False
        return True

    def active_count(self, *, scope: str, owner_id: str) -> int:
        return len(self._active.get((scope, owner_id), set()))

    def queued(self, *, scope: str, owner_id: str) -> tuple[str, ...]:
        return tuple(self._queues.get((scope, owner_id), ()))


def _valid_snapshot(snapshot: object) -> bool:
    if type(snapshot) is not ResourceSnapshot:
        return False
    try:
        cpu_percent = snapshot.cpu_percent
        memory_percent = snapshot.memory_percent
        available_memory_bytes = snapshot.available_memory_bytes
        logical_cpu_count = snapshot.logical_cpu_count
        total_memory_bytes = snapshot.total_memory_bytes
        process_rss_bytes = snapshot.process_rss_bytes
        battery_percent = snapshot.battery_percent
        power_plugged = snapshot.power_plugged
    except AttributeError:
        return False

    for value in (cpu_percent, memory_percent):
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or not 0 <= value <= 100
        ):
            return False
    if type(available_memory_bytes) is not int or available_memory_bytes < 0:
        return False
    if logical_cpu_count is not None and (
        type(logical_cpu_count) is not int or logical_cpu_count <= 0
    ):
        return False
    for value in (total_memory_bytes, process_rss_bytes):
        if value is not None and (type(value) is not int or value < 0):
            return False
    if battery_percent is not None and (
        type(battery_percent) not in (int, float)
        or not math.isfinite(battery_percent)
        or not 0 <= battery_percent <= 100
    ):
        return False
    return power_plugged is None or type(power_plugged) is bool


def _validate_budget(budget: ResourceBudget) -> None:
    if type(budget) is not ResourceBudget:
        raise ValueError("resource budget must be an exact ResourceBudget")
    if (
        type(budget.scope) is not str
        or not budget.scope.strip()
        or type(budget.owner_id) is not str
        or not budget.owner_id.strip()
    ):
        raise ValueError("resource budget scope and owner_id must not be empty")
    if (
        type(budget.max_concurrent) is not int
        or not 1 <= budget.max_concurrent <= _SQLITE_MAX_INT64
    ):
        raise ValueError("max_concurrent must be a positive SQLite-sized integer")
    for name, value in (
        ("max_cpu_percent", budget.max_cpu_percent),
        ("max_memory_percent", budget.max_memory_percent),
    ):
        if value is not None and (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or not 0 < value <= 100
        ):
            raise ValueError(f"{name} must be a finite number in the range (0, 100]")
