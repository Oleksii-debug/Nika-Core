from __future__ import annotations

from dataclasses import replace
from math import inf, nan
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.resources import ResourceBudget, ResourceManager, ResourceSnapshot
from nika_core.resources.manager import (
    ResourceTelemetryError,
    _resource_pressure_reason,
    _valid_snapshot,
)


class Observer:
    def __init__(self) -> None:
        self.current: object = ResourceSnapshot(
            cpu_percent=20.0,
            memory_percent=30.0,
            available_memory_bytes=1_000_000,
        )
        self.failure: Exception | None = None

    def snapshot(self) -> ResourceSnapshot:
        if self.failure is not None:
            raise self.failure
        return self.current  # type: ignore[return-value]


def _manager(tmp_path: Path) -> tuple[ResourceManager, Observer, SQLiteStore]:
    store = SQLiteStore(tmp_path / "state.db")
    store.initialize()
    observer = Observer()
    return ResourceManager(store, observer), observer, store


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cpu_percent", nan),
        ("cpu_percent", inf),
        ("cpu_percent", -1),
        ("cpu_percent", 101),
        ("cpu_percent", True),
        ("cpu_percent", 10**1000),
        ("cpu_percent", None),
        ("memory_percent", nan),
        ("memory_percent", -inf),
        ("memory_percent", False),
        ("memory_percent", None),
        ("disk_percent", True),
        ("gpu_percent", nan),
        ("battery_percent", "80"),
        ("available_memory_bytes", None),
        ("available_memory_bytes", True),
        ("available_memory_bytes", -1),
        ("available_disk_bytes", 1.5),
        ("process_rss_bytes", True),
        ("total_memory_bytes", -1),
        ("logical_cpu_count", 0),
        ("logical_cpu_count", False),
        ("power_plugged", "yes"),
    ],
)
def test_malformed_observation_cannot_authorize_resource_work(
    field: str, value: object
) -> None:
    snapshot = replace(
        ResourceSnapshot(
            cpu_percent=20,
            memory_percent=30,
            available_memory_bytes=1_000_000,
        ),
        **{field: value},
    )
    budget = ResourceBudget(scope="agent", owner_id="worker")
    assert not _valid_snapshot(snapshot)
    assert _resource_pressure_reason(budget, snapshot) == "invalid_observation"


@pytest.mark.parametrize("value", [None, object()])
def test_non_snapshot_observation_is_rejected(value: object) -> None:
    budget = ResourceBudget(scope="agent", owner_id="worker")
    assert not _valid_snapshot(value)  # type: ignore[arg-type]
    assert _resource_pressure_reason(budget, value) == "invalid_observation"


def test_invalid_observation_fails_closed_in_request_and_status(tmp_path: Path) -> None:
    manager, observer, _ = _manager(tmp_path)
    observer.current = replace(
        observer.current, cpu_percent=nan  # type: ignore[arg-type]
    )
    decision = manager.request(scope="agent", owner_id="worker", request_id="one")
    assert (decision.granted, decision.reason) == (False, "invalid_observation")
    assert manager.active_count(scope="agent", owner_id="worker") == 0
    assert manager.queued(scope="agent", owner_id="worker") == ("one",)
    with pytest.raises(ResourceTelemetryError, match="resource telemetry invalid"):
        manager.status(scope="agent", owner_id="worker")


def test_observer_exception_is_contained_without_losing_waiting_request(
    tmp_path: Path,
) -> None:
    manager, observer, _ = _manager(tmp_path)
    observer.failure = RuntimeError("private host diagnostic")
    denied = manager.request(scope="agent", owner_id="worker", request_id="one")
    assert (denied.granted, denied.reason) == (False, "invalid_observation")
    assert manager.queued(scope="agent", owner_id="worker") == ("one",)
    with pytest.raises(ResourceTelemetryError) as exc:
        manager.status(scope="agent", owner_id="worker")
    assert "private host diagnostic" not in str(exc.value)
    observer.failure = None
    assert manager.request(scope="agent", owner_id="worker", request_id="one").granted


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_cpu_percent", 10**1000),
        ("max_cpu_percent", True),
        ("max_memory_percent", nan),
        ("max_disk_percent", inf),
        ("max_gpu_percent", -1),
    ],
)
def test_invalid_new_budget_fails_before_persistence(
    tmp_path: Path, field: str, value: object
) -> None:
    manager, _, _ = _manager(tmp_path)
    budget = replace(ResourceBudget(scope="agent", owner_id="worker"), **{field: value})
    with pytest.raises(ValueError):
        manager.set_budget(budget)
    assert manager.get_budget(scope="agent", owner_id="worker") == ResourceBudget(
        scope="agent", owner_id="worker"
    )


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("max_concurrent", 1.5),
        ("max_cpu_percent", inf),
        ("max_memory_percent", "invalid"),
        ("max_process_memory_bytes", 1.5),
    ],
)
def test_corrupt_persisted_budget_cannot_grant_work(
    tmp_path: Path, column: str, value: object
) -> None:
    manager, _, store = _manager(tmp_path)
    manager.set_budget(ResourceBudget(scope="agent", owner_id="worker"))
    with store.connection() as conn:
        conn.execute(
            f"UPDATE resource_budgets SET {column} = ? WHERE scope = ? AND owner_id = ?",
            (value, "agent", "worker"),
        )
    with pytest.raises(ValueError):
        manager.request(scope="agent", owner_id="worker", request_id="one")
    assert manager.active_count(scope="agent", owner_id="worker") == 0


def test_valid_boundary_observation_still_grants(tmp_path: Path) -> None:
    manager, observer, _ = _manager(tmp_path)
    manager.set_budget(
        ResourceBudget(
            scope="agent",
            owner_id="worker",
            max_cpu_percent=100,
            max_memory_percent=100,
        )
    )
    observer.current = ResourceSnapshot(
        cpu_percent=100,
        memory_percent=100,
        available_memory_bytes=0,
        disk_percent=0,
        gpu_percent=0,
        power_plugged=False,
    )
    assert manager.request(scope="agent", owner_id="worker", request_id="one").granted
    assert manager.status(scope="agent", owner_id="worker").cpu_headroom_percent == 0


@pytest.mark.parametrize(
    "missing_field",
    (
        "cpu_percent",
        "memory_percent",
        "available_memory_bytes",
        "disk_percent",
        "logical_cpu_count",
        "power_plugged",
    ),
)
def test_incomplete_snapshot_fails_closed_before_resource_admission(
    missing_field: str,
) -> None:
    snapshot = ResourceSnapshot(
        cpu_percent=20, memory_percent=30, available_memory_bytes=1_000_000
    )
    object.__delattr__(snapshot, missing_field)
    assert not _valid_snapshot(snapshot)
    budget = ResourceBudget(scope="agent", owner_id="worker")
    assert _resource_pressure_reason(budget, snapshot) == "invalid_observation"


def test_uninitialized_snapshot_fails_closed() -> None:
    incomplete = object.__new__(ResourceSnapshot)
    budget = ResourceBudget(scope="agent", owner_id="worker")
    assert not _valid_snapshot(incomplete)
    assert _resource_pressure_reason(budget, incomplete) == "invalid_observation"


def test_incomplete_host_sample_keeps_queued_work_and_sanitizes_status(
    tmp_path: Path,
) -> None:
    manager, observer, _ = _manager(tmp_path)
    sample = ResourceSnapshot(
        cpu_percent=20, memory_percent=30, available_memory_bytes=1_000_000
    )
    object.__delattr__(sample, "battery_percent")
    observer.current = sample

    decision = manager.request(scope="agent", owner_id="worker", request_id="one")
    assert (decision.granted, decision.reason) == (False, "invalid_observation")
    assert manager.active_count(scope="agent", owner_id="worker") == 0
    assert manager.queued(scope="agent", owner_id="worker") == ("one",)
    with pytest.raises(ResourceTelemetryError, match="resource telemetry invalid"):
        manager.status(scope="agent", owner_id="worker")

    observer.current = ResourceSnapshot(
        cpu_percent=20, memory_percent=30, available_memory_bytes=1_000_000
    )
    assert manager.request(scope="agent", owner_id="worker", request_id="one").granted


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("max_concurrent", 1 << 63),
        ("max_concurrent", 10**1000),
        ("max_process_memory_bytes", 1 << 63),
        ("max_process_memory_bytes", 10**1000),
    ),
)
def test_sqlite_unrepresentable_budget_is_rejected_before_write(
    tmp_path: Path, field: str, value: int
) -> None:
    manager, _, _ = _manager(tmp_path)
    budget = replace(ResourceBudget(scope="agent", owner_id="worker"), **{field: value})
    with pytest.raises(ValueError, match="SQLite-sized integer"):
        manager.set_budget(budget)
    assert manager.get_budget(scope="agent", owner_id="worker") == ResourceBudget(
        scope="agent", owner_id="worker"
    )


def test_sqlite_integer_boundary_round_trips_without_truncation(tmp_path: Path) -> None:
    manager, _, _ = _manager(tmp_path)
    budget = ResourceBudget(
        scope="agent",
        owner_id="worker",
        max_concurrent=(1 << 63) - 1,
        max_process_memory_bytes=(1 << 63) - 1,
    )
    manager.set_budget(budget)
    assert manager.get_budget(scope="agent", owner_id="worker") == budget
