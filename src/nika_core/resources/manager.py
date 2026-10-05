from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime

from nika_core.data.sqlite import SQLiteStore
from nika_core.resources.contracts import ResourceBudget, ResourceObserverPort


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


def _validate_budget(budget: ResourceBudget) -> None:
    if not budget.scope.strip() or not budget.owner_id.strip():
        raise ValueError("resource budget scope and owner_id must not be empty")
    if budget.max_concurrent <= 0:
        raise ValueError("max_concurrent must be greater than zero")
    for name, value in (
        ("max_cpu_percent", budget.max_cpu_percent),
        ("max_memory_percent", budget.max_memory_percent),
    ):
        if value is not None and not 0 < value <= 100:
            raise ValueError(f"{name} must be in the range (0, 100]")
