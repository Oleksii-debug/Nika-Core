from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.resources import (
    PsutilResourceObserver,
    ResourceBudget,
    ResourceManager,
    ResourceSnapshot,
    ResourceTelemetryError,
    psutil_adapter,
)


class FakeObserver:
    def __init__(self, snapshot: ResourceSnapshot) -> None:
        self.value = snapshot
        self.calls = 0

    def snapshot(self) -> ResourceSnapshot:
        self.calls += 1
        return self.value


def _store(tmp_path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "Ніка resource telemetry.db")
    store.initialize()
    return store


def test_resource_snapshot_keeps_legacy_three_field_construction() -> None:
    snapshot = ResourceSnapshot(
        cpu_percent=10.0,
        memory_percent=20.0,
        available_memory_bytes=3_000_000_000,
    )
    assert snapshot.logical_cpu_count is None
    assert snapshot.total_memory_bytes is None
    assert snapshot.process_rss_bytes is None
    assert snapshot.battery_percent is None
    assert snapshot.power_plugged is None


def test_resource_status_reports_headroom_without_mutating_admission_state(tmp_path) -> None:
    observer = FakeObserver(
        ResourceSnapshot(
            cpu_percent=42.5,
            memory_percent=55.0,
            available_memory_bytes=5_000_000_000,
            logical_cpu_count=12,
            total_memory_bytes=16_000_000_000,
            process_rss_bytes=250_000_000,
            battery_percent=73.0,
            power_plugged=False,
        )
    )
    manager = ResourceManager(_store(tmp_path), observer)
    manager.set_budget(
        ResourceBudget(
            scope="workspace",
            owner_id="research",
            max_concurrent=2,
            max_cpu_percent=80.0,
            max_memory_percent=85.0,
        )
    )

    assert manager.request(
        scope="workspace", owner_id="research", request_id="active"
    ).granted
    before_queue = manager.queued(scope="workspace", owner_id="research")

    status = manager.status(scope="workspace", owner_id="research")

    assert status.active_count == 1
    assert status.queued_count == 0
    assert status.concurrency_headroom == 1
    assert status.cpu_headroom_percent == pytest.approx(37.5)
    assert status.memory_headroom_percent == pytest.approx(30.0)
    assert status.pressure_reasons == ()
    assert not status.under_pressure
    assert status.snapshot.logical_cpu_count == 12
    assert status.snapshot.battery_percent == 73.0
    assert manager.queued(scope="workspace", owner_id="research") == before_queue
    assert manager.active_count(scope="workspace", owner_id="research") == 1


def test_resource_status_reports_all_current_budget_pressure(tmp_path) -> None:
    observer = FakeObserver(
        ResourceSnapshot(
            cpu_percent=10.0,
            memory_percent=20.0,
            available_memory_bytes=8_000_000_000,
        )
    )
    manager = ResourceManager(_store(tmp_path), observer)
    manager.set_budget(
        ResourceBudget(
            scope="agent",
            owner_id="heavy",
            max_concurrent=1,
            max_cpu_percent=80.0,
            max_memory_percent=75.0,
        )
    )
    assert manager.request(scope="agent", owner_id="heavy", request_id="first").granted
    observer.value = ResourceSnapshot(
        cpu_percent=91.5,
        memory_percent=88.0,
        available_memory_bytes=1_000_000_000,
    )

    status = manager.status(scope="agent", owner_id="heavy")

    assert status.concurrency_headroom == 0
    assert status.cpu_headroom_percent == pytest.approx(-11.5)
    assert status.memory_headroom_percent == pytest.approx(-13.0)
    assert status.pressure_reasons == (
        "concurrency_limit",
        "cpu_limit",
        "memory_limit",
    )
    assert status.under_pressure


def test_psutil_observer_normalizes_host_process_and_power_measurements(monkeypatch) -> None:
    monkeypatch.setattr(psutil_adapter.psutil, "cpu_percent", lambda *, interval: 24.5)
    monkeypatch.setattr(psutil_adapter.psutil, "cpu_count", lambda *, logical: 12)
    monkeypatch.setattr(
        psutil_adapter.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(percent=61.25, available=6_000, total=16_000),
    )
    monkeypatch.setattr(
        psutil_adapter.psutil,
        "sensors_battery",
        lambda: SimpleNamespace(percent=67.0, power_plugged=True),
    )
    process = SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=1_234))

    snapshot = PsutilResourceObserver(process=process).snapshot()

    assert snapshot == ResourceSnapshot(
        cpu_percent=24.5,
        memory_percent=61.25,
        available_memory_bytes=6_000,
        logical_cpu_count=12,
        total_memory_bytes=16_000,
        process_rss_bytes=1_234,
        battery_percent=67.0,
        power_plugged=True,
    )


def test_psutil_observer_keeps_optional_telemetry_unavailable_instead_of_guessing(
    monkeypatch,
) -> None:
    monkeypatch.setattr(psutil_adapter.psutil, "cpu_percent", lambda *, interval: 0.0)
    monkeypatch.setattr(psutil_adapter.psutil, "cpu_count", lambda *, logical: None)
    monkeypatch.setattr(
        psutil_adapter.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(percent=20.0, available=8_000, total=10_000),
    )
    monkeypatch.setattr(psutil_adapter.psutil, "sensors_battery", lambda: None)

    class UnreadableProcess:
        def memory_info(self):
            raise OSError("fixture unavailable")

    snapshot = PsutilResourceObserver(process=UnreadableProcess()).snapshot()

    assert snapshot.logical_cpu_count is None
    assert snapshot.process_rss_bytes is None
    assert snapshot.battery_percent is None
    assert snapshot.power_plugged is None


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("cpu_percent", float("nan")),
        ("cpu_percent", float("inf")),
        ("cpu_percent", True),
        ("cpu_percent", -1.0),
        ("cpu_percent", 100.1),
        ("memory_percent", True),
        ("memory_percent", -1.0),
        ("available_memory_bytes", True),
        ("available_memory_bytes", -1),
        ("logical_cpu_count", True),
        ("logical_cpu_count", 0),
        ("total_memory_bytes", True),
        ("total_memory_bytes", -1),
        ("process_rss_bytes", True),
        ("process_rss_bytes", -1),
        ("battery_percent", True),
        ("battery_percent", 101.0),
        ("power_plugged", 1),
    ),
)
def test_invalid_snapshot_fails_closed_for_admission_status_and_revalidation(
    tmp_path,
    field_name: str,
    value: object,
) -> None:
    valid = ResourceSnapshot(
        cpu_percent=20.0,
        memory_percent=30.0,
        available_memory_bytes=4_000_000_000,
        logical_cpu_count=8,
        total_memory_bytes=8_000_000_000,
        process_rss_bytes=100_000_000,
        battery_percent=60.0,
        power_plugged=True,
    )
    observer = FakeObserver(valid)
    manager = ResourceManager(_store(tmp_path), observer)
    manager.set_budget(
        ResourceBudget(
            scope="training",
            owner_id="owner",
            max_cpu_percent=80.0,
            max_memory_percent=80.0,
        )
    )
    assert manager.request(
        scope="training", owner_id="owner", request_id="active"
    ).granted

    observer.value = replace(valid, **{field_name: value})

    revalidated = manager.revalidate(
        scope="training", owner_id="owner", request_id="active"
    )
    assert (revalidated.granted, revalidated.reason) == (
        False,
        "invalid_observation",
    )
    assert manager.active_count(scope="training", owner_id="owner") == 1
    with pytest.raises(ResourceTelemetryError, match="resource telemetry invalid"):
        manager.status(scope="training", owner_id="owner")

    manager.release(scope="training", owner_id="owner", request_id="active")
    waiting = manager.request(
        scope="training", owner_id="owner", request_id="waiting"
    )
    assert (waiting.granted, waiting.reason, waiting.queue_position) == (
        False,
        "invalid_observation",
        1,
    )
    assert manager.queued(scope="training", owner_id="owner") == ("waiting",)

    observer.value = valid
    recovered = manager.request(
        scope="training", owner_id="owner", request_id="waiting"
    )
    assert recovered.granted
    assert manager.queued(scope="training", owner_id="owner") == ()


@pytest.mark.parametrize(
    "missing_field",
    (
        "cpu_percent",
        "memory_percent",
        "available_memory_bytes",
        "logical_cpu_count",
        "total_memory_bytes",
        "process_rss_bytes",
        "battery_percent",
        "power_plugged",
    ),
)
def test_incomplete_snapshot_is_sanitized_and_never_grants(
    tmp_path,
    missing_field: str,
) -> None:
    sample = ResourceSnapshot(
        cpu_percent=20.0,
        memory_percent=30.0,
        available_memory_bytes=4_000_000_000,
    )
    object.__delattr__(sample, missing_field)
    observer = FakeObserver(sample)
    manager = ResourceManager(_store(tmp_path), observer)

    decision = manager.request(
        scope="agent", owner_id="worker", request_id="waiting"
    )

    assert (decision.granted, decision.reason) == (False, "invalid_observation")
    assert manager.queued(scope="agent", owner_id="worker") == ("waiting",)
    with pytest.raises(ResourceTelemetryError, match="resource telemetry invalid"):
        manager.status(scope="agent", owner_id="worker")


def test_non_snapshot_observer_result_fails_closed_without_attribute_leak(
    tmp_path,
) -> None:
    observer = FakeObserver(
        ResourceSnapshot(
            cpu_percent=20.0,
            memory_percent=30.0,
            available_memory_bytes=4_000_000_000,
        )
    )
    observer.value = object()  # type: ignore[assignment]
    manager = ResourceManager(_store(tmp_path), observer)

    decision = manager.request(
        scope="agent", owner_id="worker", request_id="waiting"
    )

    assert (decision.granted, decision.reason) == (False, "invalid_observation")
    with pytest.raises(ResourceTelemetryError, match="resource telemetry invalid"):
        manager.status(scope="agent", owner_id="worker")


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("max_concurrent", True),
        ("max_concurrent", 0),
        ("max_concurrent", 1 << 63),
        ("max_cpu_percent", True),
        ("max_cpu_percent", float("nan")),
        ("max_cpu_percent", float("inf")),
        ("max_memory_percent", True),
        ("max_memory_percent", "80"),
    ),
)
def test_resource_budget_rejects_noncanonical_or_unstorable_limits(
    tmp_path,
    field_name: str,
    value: object,
) -> None:
    manager = ResourceManager(
        _store(tmp_path),
        FakeObserver(
            ResourceSnapshot(
                cpu_percent=20.0,
                memory_percent=30.0,
                available_memory_bytes=4_000_000_000,
            )
        ),
    )
    malformed = replace(
        ResourceBudget(scope="agent", owner_id="worker"),
        **{field_name: value},
    )

    with pytest.raises(ValueError):
        manager.set_budget(malformed)  # type: ignore[arg-type]

    assert manager.get_budget(scope="agent", owner_id="worker") == ResourceBudget(
        scope="agent",
        owner_id="worker",
    )


def test_sqlite_max_concurrency_boundary_round_trips(tmp_path) -> None:
    manager = ResourceManager(
        _store(tmp_path),
        FakeObserver(
            ResourceSnapshot(
                cpu_percent=20.0,
                memory_percent=30.0,
                available_memory_bytes=4_000_000_000,
            )
        ),
    )
    budget = ResourceBudget(
        scope="agent",
        owner_id="worker",
        max_concurrent=(1 << 63) - 1,
    )

    manager.set_budget(budget)

    assert manager.get_budget(scope="agent", owner_id="worker") == budget

