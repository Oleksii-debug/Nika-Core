from __future__ import annotations

from dataclasses import replace
from math import inf, nan
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.resources import ResourceBudget, ResourceManager, ResourceSnapshot
from nika_core.resources.manager import ResourceTelemetryError


class Observer:
    def __init__(self, snapshot: ResourceSnapshot) -> None:
        self.current = snapshot
        self.error: Exception | None = None

    def snapshot(self) -> ResourceSnapshot:
        if self.error is not None:
            raise self.error
        return self.current


def _manager(tmp_path: Path) -> tuple[ResourceManager, Observer]:
    store = SQLiteStore(tmp_path / "state.db")
    store.initialize()
    observer = Observer(
        ResourceSnapshot(
            cpu_percent=20.0,
            memory_percent=30.0,
            available_memory_bytes=1_000_000,
        )
    )
    return ResourceManager(store, observer), observer


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("cpu_percent", nan),
        ("cpu_percent", inf),
        ("cpu_percent", -1.0),
        ("cpu_percent", 101.0),
        ("cpu_percent", True),
        ("cpu_percent", "20"),
        ("memory_percent", nan),
        ("memory_percent", -inf),
        ("memory_percent", -1.0),
        ("memory_percent", 101.0),
        ("memory_percent", False),
        ("available_memory_bytes", -1),
        ("available_memory_bytes", True),
        ("available_memory_bytes", 1.5),
    ],
)
def test_invalid_telemetry_denies_unconstrained_admission_and_status(
    tmp_path: Path, field: str, bad: object
) -> None:
    manager, observer = _manager(tmp_path)
    observer.current = replace(observer.current, **{field: bad})
    decision = manager.request(scope="agent", owner_id="a", request_id="work")
    assert decision.granted is False
    assert decision.reason == "telemetry_unavailable"
    assert manager.active_count(scope="agent", owner_id="a") == 0
    assert manager.queued(scope="agent", owner_id="a") == ("work",)
    with pytest.raises(ResourceTelemetryError, match="resource telemetry invalid"):
        manager.status(scope="agent", owner_id="a")


def test_observer_exception_does_not_leak_and_recovery_preserves_fifo(tmp_path: Path) -> None:
    manager, observer = _manager(tmp_path)
    observer.error = RuntimeError("private host diagnostic")
    denied = manager.request(scope="workspace", owner_id="w", request_id="first")
    assert (denied.granted, denied.reason) == (False, "telemetry_unavailable")
    assert manager.queued(scope="workspace", owner_id="w") == ("first",)
    with pytest.raises(ResourceTelemetryError) as error:
        manager.status(scope="workspace", owner_id="w")
    assert "private host diagnostic" not in str(error.value)

    observer.error = None
    later = manager.request(scope="workspace", owner_id="w", request_id="later")
    assert (later.granted, later.reason, later.queue_position) == (False, "fifo_wait", 2)
    winner = manager.request(scope="workspace", owner_id="w", request_id="first")
    assert (winner.granted, winner.reason) == (True, "granted")
    assert manager.active_count(scope="workspace", owner_id="w") == 1


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("max_concurrent", True),
        ("max_concurrent", 1.0),
        ("max_concurrent", 0),
        ("max_cpu_percent", nan),
        ("max_cpu_percent", inf),
        ("max_cpu_percent", True),
        ("max_cpu_percent", 0),
        ("max_cpu_percent", -1),
        ("max_cpu_percent", 101),
        ("max_memory_percent", nan),
        ("max_memory_percent", -inf),
        ("max_memory_percent", False),
        ("max_memory_percent", 0),
    ],
)
def test_invalid_budget_is_rejected_before_durable_write(
    tmp_path: Path, field: str, bad: object
) -> None:
    manager, _ = _manager(tmp_path)
    budget = replace(ResourceBudget(scope="agent", owner_id="a"), **{field: bad})
    with pytest.raises(ValueError):
        manager.set_budget(budget)
    assert manager.get_budget(scope="agent", owner_id="a") == ResourceBudget(
        scope="agent", owner_id="a"
    )


def test_valid_boundary_telemetry_and_limits_are_preserved(tmp_path: Path) -> None:
    manager, observer = _manager(tmp_path)
    manager.set_budget(
        ResourceBudget(
            scope="agent",
            owner_id="a",
            max_concurrent=1,
            max_cpu_percent=100,
            max_memory_percent=100,
        )
    )
    observer.current = replace(
        observer.current, cpu_percent=100, memory_percent=100, available_memory_bytes=0
    )
    assert manager.request(scope="agent", owner_id="a", request_id="one").granted
    status = manager.status(scope="agent", owner_id="a")
    assert status.pressure_reasons == ("concurrency_limit",)
    assert status.cpu_headroom_percent == 0
    assert status.memory_headroom_percent == 0
    assert manager.request(scope="agent", owner_id="a", request_id="two").reason == (
        "concurrency_limit"
    )
