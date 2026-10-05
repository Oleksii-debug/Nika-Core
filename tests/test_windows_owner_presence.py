from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

import nika_core.windows_owner_presence as presence_module
from nika_core.background_life import (
    BackgroundAction,
    BackgroundWorkKind,
    OwnerPresence,
)
from nika_core.background_runtime import BackgroundDispatchGuard
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.resources import ResourceBudget, ResourceManager, ResourceSnapshot
from nika_core.windows_owner_presence import Win32LastInputApi, WindowsOwnerPresenceObserver


class FakeLastInputApi:
    def __init__(
        self,
        *,
        last_ticks: list[object],
        current_ticks: list[object],
    ) -> None:
        self._last_ticks = list(last_ticks)
        self._current_ticks = list(current_ticks)

    def get_last_input_tick_ms(self) -> int:
        value = self._last_ticks.pop(0)
        return value  # type: ignore[return-value]

    def get_tick_count64_ms(self) -> int:
        value = self._current_ticks.pop(0)
        return value  # type: ignore[return-value]


class ExplodingApi:
    def get_last_input_tick_ms(self) -> int:
        raise OSError("synthetic win32 failure")

    def get_tick_count64_ms(self) -> int:  # pragma: no cover - first call already fails
        raise AssertionError("must not be called")


class StableResourceObserver:
    def snapshot(self) -> ResourceSnapshot:
        return ResourceSnapshot(
            cpu_percent=10.0,
            memory_percent=20.0,
            available_memory_bytes=2_000_000_000,
            power_plugged=True,
        )


def _audit(tmp_path: Path, filename: str = "nika.db") -> AuditLog:
    store = SQLiteStore(tmp_path / filename)
    store.initialize()
    return AuditLog(store)


def _observer(
    tmp_path: Path,
    *,
    last_ticks: list[object],
    current_ticks: list[object],
    away_after_seconds: float = 60,
    now: datetime | None = None,
) -> tuple[WindowsOwnerPresenceObserver, AuditLog]:
    audit = _audit(tmp_path)
    clock_value = now or datetime(2030, 1, 1, tzinfo=UTC)
    observer = WindowsOwnerPresenceObserver(
        audit,
        away_after_seconds=away_after_seconds,
        api=FakeLastInputApi(last_ticks=last_ticks, current_ticks=current_ticks),
        clock=lambda: clock_value,
    )
    return observer, audit


def test_recent_input_is_active_and_sample_audit_is_privacy_minimized(tmp_path: Path) -> None:
    observer, audit = _observer(
        tmp_path,
        last_ticks=[100_000, 100_000],
        current_ticks=[130_000],
        away_after_seconds=60,
    )

    observation = observer.observe()

    assert observation.presence is OwnerPresence.ACTIVE
    assert observation.source_id == "win32-owner-presence"
    assert observation.sequence > 0
    sampled = audit.list_for(
        entity_type="owner_presence_source",
        entity_id="win32-owner-presence",
    )
    assert len(sampled) == 1
    assert sampled[0].event_type == "background.owner_presence_sampled"
    assert sampled[0].payload == {
        "presence": "active",
        "probe": "win32_last_input",
        "threshold_ms": 60_000,
    }
    serialized = repr(sampled[0].payload)
    assert "100000" not in serialized
    assert "130000" not in serialized
    assert "window" not in serialized
    assert "process" not in serialized
    assert "key" not in serialized


def test_physical_observer_drives_canonical_background_dispatch_guard(tmp_path: Path) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = SQLiteStore(tmp_path / "integration.db")
    store.initialize()
    audit = AuditLog(store)
    queue = TaskQueue(store)
    resources = ResourceManager(store, StableResourceObserver())
    resources.set_budget(
        ResourceBudget(
            scope="background_life",
            owner_id="living-agent",
            max_concurrent=1,
            max_cpu_percent=80.0,
            max_memory_percent=80.0,
        )
    )
    observer = WindowsOwnerPresenceObserver(
        audit,
        away_after_seconds=60,
        api=FakeLastInputApi(
            last_ticks=[1_000] * 12,
            current_ticks=[40_000, 100_000, 100_000, 100_000, 100_000, 100_000],
        ),
        clock=lambda: now,
    )
    guard = BackgroundDispatchGuard(
        queue=queue,
        audit=audit,
        resources=resources,
        presence=observer,
        source_id=observer.source_id,
        clock=lambda: now,
    )
    task = queue.create(workspace_id="living", agent_id="nika")
    queue.transition(task.task_id, TaskState.READY)
    calls: list[str] = []

    assert observer.observe().presence is OwnerPresence.ACTIVE

    async def effect() -> object:
        calls.append("run")
        return "ok"

    result = asyncio.run(
        guard.dispatch(
            task_id=task.task_id,
            work_kind=BackgroundWorkKind.READING_RESEARCH,
            effect=effect,
        )
    )

    assert result.action is BackgroundAction.RUN
    assert result.executed is True
    assert result.effect_result == "ok"
    assert calls == ["run"]


def test_idle_at_threshold_is_away_after_stability_probation(tmp_path: Path) -> None:
    observer, _audit_log = _observer(
        tmp_path,
        last_ticks=[20_000, 20_000, 20_000, 20_000],
        current_ticks=[80_000, 140_000],
        away_after_seconds=60,
    )

    assert observer.observe().presence is OwnerPresence.ACTIVE
    assert observer.observe().presence is OwnerPresence.AWAY


def test_fractional_threshold_rounds_up_to_millisecond(tmp_path: Path) -> None:
    observer, _audit_log = _observer(
        tmp_path,
        last_ticks=[10, 10],
        current_ticks=[11],
        away_after_seconds=0.0011,
    )

    assert observer.away_after_seconds == 0.002
    assert observer.observe().presence is OwnerPresence.ACTIVE


def test_dword_wrap_is_classified_without_false_recent_arithmetic(tmp_path: Path) -> None:
    wrap = 1 << 32
    observer, _audit_log = _observer(
        tmp_path,
        last_ticks=[0xFFFFFF00] * 4,
        current_ticks=[wrap + 1_000, wrap + 2_000],
        away_after_seconds=1,
    )

    assert observer.observe().presence is OwnerPresence.ACTIVE
    assert observer.observe().presence is OwnerPresence.AWAY


def test_input_arriving_during_probe_forces_active(tmp_path: Path) -> None:
    observer, _audit_log = _observer(
        tmp_path,
        last_ticks=[1_000, 99_999],
        current_ticks=[100_000],
        away_after_seconds=10,
    )

    assert observer.observe().presence is OwnerPresence.ACTIVE


def test_large_uptime_uses_low_dword_without_losing_safe_away_classification(
    tmp_path: Path,
) -> None:
    wrap = 1 << 32
    observer, _audit_log = _observer(
        tmp_path,
        last_ticks=[500] * 4,
        current_ticks=[3 * wrap + 2_000, 3 * wrap + 3_000],
        away_after_seconds=1,
    )

    assert observer.observe().presence is OwnerPresence.ACTIVE
    assert observer.observe().presence is OwnerPresence.AWAY


@pytest.mark.parametrize(
    ("last_ticks", "current_ticks", "error_type", "message"),
    [
        ([True, True], [100], TypeError, "last input tick before sample"),
        ([-1, -1], [100], ValueError, "Win32 DWORD range"),
        ([(1 << 32), (1 << 32)], [100], ValueError, "Win32 DWORD range"),
        ([1, 1], [True], TypeError, "current tick"),
        ([1, 1], [-1], ValueError, "current tick"),
        ([1, 1], [(1 << 64)], ValueError, "current tick"),
    ],
)
def test_hostile_or_out_of_range_tick_carriers_fail_closed_before_audit(
    tmp_path: Path,
    last_ticks: list[object],
    current_ticks: list[object],
    error_type: type[Exception],
    message: str,
) -> None:
    observer, audit = _observer(
        tmp_path,
        last_ticks=last_ticks,
        current_ticks=current_ticks,
    )

    with pytest.raises(error_type, match=message):
        observer.observe()

    assert audit.list_for(
        entity_type="owner_presence_source",
        entity_id="win32-owner-presence",
    ) == ()


def test_win32_api_failure_propagates_without_minting_presence_evidence(tmp_path: Path) -> None:
    audit = _audit(tmp_path)
    observer = WindowsOwnerPresenceObserver(
        audit,
        away_after_seconds=60,
        api=ExplodingApi(),
    )

    with pytest.raises(OSError, match="synthetic win32 failure"):
        observer.observe()

    assert audit.list_for(
        entity_type="owner_presence_source",
        entity_id="win32-owner-presence",
    ) == ()


def test_sequence_is_durable_and_advances_across_fresh_store_and_observer(
    tmp_path: Path,
) -> None:
    path = tmp_path / "nika.db"
    first_store = SQLiteStore(path)
    first_store.initialize()
    first = WindowsOwnerPresenceObserver(
        AuditLog(first_store),
        away_after_seconds=60,
        api=FakeLastInputApi(last_ticks=[10, 10], current_ticks=[20]),
        clock=lambda: datetime(2030, 1, 1, tzinfo=UTC),
    )
    first_observation = first.observe()

    first_audit = AuditLog(first_store)
    first_audit.append(
        event_type="unrelated.event",
        entity_type="test",
        entity_id="other",
        payload={"ok": True},
    )

    restarted_store = SQLiteStore(path)
    restarted_store.initialize()
    restarted = WindowsOwnerPresenceObserver(
        AuditLog(restarted_store),
        away_after_seconds=60,
        api=FakeLastInputApi(last_ticks=[20, 20], current_ticks=[30]),
        clock=lambda: datetime(2030, 1, 1, 0, 0, 1, tzinfo=UTC),
    )
    restarted_observation = restarted.observe()

    assert restarted_observation.sequence > first_observation.sequence


def test_clock_must_be_exact_timezone_aware_utc_and_mints_no_event_on_failure(
    tmp_path: Path,
) -> None:
    class DatetimeSubclass(datetime):
        pass

    values: list[object] = [
        "2030-01-01T00:00:00Z",
        DatetimeSubclass(2030, 1, 1, tzinfo=UTC),
        datetime(2030, 1, 1, tzinfo=UTC).replace(tzinfo=None),
        datetime(2030, 1, 1, tzinfo=timezone(timedelta(hours=1))),
    ]
    expected: list[tuple[type[Exception], str]] = [
        (TypeError, "exact built-in datetime"),
        (TypeError, "exact built-in datetime"),
        (ValueError, "timezone-aware"),
        (ValueError, "must use UTC"),
    ]

    for index, (clock_value, (error_type, message)) in enumerate(
        zip(values, expected, strict=True)
    ):
        audit = _audit(tmp_path, f"clock-{index}.db")
        observer = WindowsOwnerPresenceObserver(
            audit,
            away_after_seconds=60,
            api=FakeLastInputApi(last_ticks=[1, 1], current_ticks=[2]),
            clock=lambda clock_value=clock_value: clock_value,  # type: ignore[return-value]
        )
        with pytest.raises(error_type, match=message):
            observer.observe()
        assert audit.list_for(
            entity_type="owner_presence_source",
            entity_id="win32-owner-presence",
        ) == ()


@pytest.mark.parametrize(
    ("value", "error_type", "message"),
    [
        (True, TypeError, "exact built-in int or float"),
        ("60", TypeError, "exact built-in int or float"),
        (0, ValueError, "greater than zero"),
        (-1, ValueError, "greater than zero"),
        (float("nan"), ValueError, "finite"),
        (float("inf"), ValueError, "finite"),
        ((1 << 32) / 1000, ValueError, "wrap-safe"),
        (10**1000, ValueError, "wrap-safe"),
    ],
)
def test_away_threshold_rejects_noncanonical_or_unsafe_values(
    tmp_path: Path,
    value: object,
    error_type: type[Exception],
    message: str,
) -> None:
    audit = _audit(tmp_path)

    with pytest.raises(error_type, match=message):
        WindowsOwnerPresenceObserver(
            audit,
            away_after_seconds=value,  # type: ignore[arg-type]
            api=FakeLastInputApi(last_ticks=[1, 1], current_ticks=[2]),
        )


def test_source_id_requires_exact_trimmed_string(tmp_path: Path) -> None:
    class StringSubclass(str):
        pass

    audit = _audit(tmp_path)

    with pytest.raises(TypeError, match="source_id"):
        WindowsOwnerPresenceObserver(
            audit,
            away_after_seconds=60,
            source_id=StringSubclass("win32-owner-presence"),
            api=FakeLastInputApi(last_ticks=[1, 1], current_ticks=[2]),
        )
    with pytest.raises(ValueError, match="source_id"):
        WindowsOwnerPresenceObserver(
            audit,
            away_after_seconds=60,
            source_id=" win32-owner-presence ",
            api=FakeLastInputApi(last_ticks=[1, 1], current_ticks=[2]),
        )
    with pytest.raises(ValueError, match="too long"):
        WindowsOwnerPresenceObserver(
            audit,
            away_after_seconds=60,
            source_id="x" * 257,
            api=FakeLastInputApi(last_ticks=[1, 1], current_ticks=[2]),
        )


@pytest.mark.parametrize(
    "source_id",
    [
        "win32\nowner",
        "win32\x00owner",
        "win32\u202eowner",
    ],
)
def test_source_id_rejects_control_and_format_characters(
    tmp_path: Path,
    source_id: str,
) -> None:
    audit = _audit(tmp_path)

    with pytest.raises(ValueError, match="control or format"):
        WindowsOwnerPresenceObserver(
            audit,
            away_after_seconds=60,
            source_id=source_id,
            api=FakeLastInputApi(last_ticks=[1, 1], current_ticks=[2]),
        )

    assert audit.list_for(
        entity_type="owner_presence_source",
        entity_id=source_id,
    ) == ()


def test_custom_source_id_is_bound_into_observation_and_audit(tmp_path: Path) -> None:
    audit = _audit(tmp_path)
    observer = WindowsOwnerPresenceObserver(
        audit,
        away_after_seconds=60,
        source_id="physical-owner-presence",
        api=FakeLastInputApi(last_ticks=[1, 1], current_ticks=[2]),
        clock=lambda: datetime(2030, 1, 1, tzinfo=UTC),
    )

    observation = observer.observe()

    assert observation.source_id == "physical-owner-presence"
    assert len(
        audit.list_for(
            entity_type="owner_presence_source",
            entity_id="physical-owner-presence",
        )
    ) == 1


def test_default_physical_api_fails_cleanly_when_not_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit = _audit(tmp_path)
    monkeypatch.setattr(presence_module.os, "name", "posix")

    with pytest.raises(OSError, match="only on Windows"):
        WindowsOwnerPresenceObserver(audit, away_after_seconds=60)


def test_api_is_sampled_last_current_last_in_that_order(tmp_path: Path) -> None:
    calls: list[str] = []

    class OrderedApi:
        def get_last_input_tick_ms(self) -> int:
            calls.append("last")
            return 1

        def get_tick_count64_ms(self) -> int:
            calls.append("current")
            return 2

    audit = _audit(tmp_path)
    observer = WindowsOwnerPresenceObserver(
        audit,
        away_after_seconds=60,
        api=OrderedApi(),
        clock=lambda: datetime(2030, 1, 1, tzinfo=UTC),
    )

    observer.observe()

    assert calls == ["last", "current", "last"]


def test_concurrent_observers_allocate_unique_durable_sequences(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "concurrent.db")
    store.initialize()
    audit = AuditLog(store)

    def sample(index: int) -> int:
        observer = WindowsOwnerPresenceObserver(
            audit,
            away_after_seconds=60,
            api=FakeLastInputApi(
                last_ticks=[index, index],
                current_ticks=[100_000 + index],
            ),
            clock=lambda: datetime(2030, 1, 1, tzinfo=UTC),
        )
        return observer.observe().sequence

    with ThreadPoolExecutor(max_workers=4) as pool:
        sequences = list(pool.map(sample, range(8)))

    assert len(set(sequences)) == 8
    assert sorted(sequences) == list(range(min(sequences), min(sequences) + 8))

    events = audit.list_for(
        entity_type="owner_presence_source",
        entity_id="win32-owner-presence",
    )
    assert len(events) == 8
    assert {event.event_id for event in events} == set(sequences)


@pytest.mark.skipif(presence_module.os.name != "nt", reason="requires Windows APIs")
def test_real_win32_last_input_api_smoke_on_windows() -> None:
    api = Win32LastInputApi()

    last_tick = api.get_last_input_tick_ms()
    current_tick = api.get_tick_count64_ms()

    assert type(last_tick) is int
    assert 0 <= last_tick < (1 << 32)
    assert type(current_tick) is int
    assert 0 <= current_tick <= (1 << 64) - 1


def test_first_epoch_future_last_input_tick_fails_closed(tmp_path: Path) -> None:
    observer, audit = _observer(
        tmp_path,
        last_ticks=[10_000, 10_000],
        current_ticks=[5_000],
        away_after_seconds=1,
    )

    with pytest.raises(ValueError, match="ahead of current tick"):
        observer.observe()

    assert audit.list_for(
        entity_type="owner_presence_source",
        entity_id="win32-owner-presence",
    ) == ()


def test_multiwrap_ambiguity_uses_minimum_idle_and_stays_conservatively_active(
    tmp_path: Path,
) -> None:
    wrap = 1 << 32
    observer, _audit_log = _observer(
        tmp_path,
        last_ticks=[1_500] * 4,
        current_ticks=[3 * wrap + 1_000, 3 * wrap + 2_000],
        away_after_seconds=1,
    )

    assert observer.observe().presence is OwnerPresence.ACTIVE
    assert observer.observe().presence is OwnerPresence.ACTIVE


def test_failed_sample_resets_stability_probation(tmp_path: Path) -> None:
    audit = _audit(tmp_path)
    now = datetime(2030, 1, 1, tzinfo=UTC)
    clock_values: list[object] = [now, RuntimeError("synthetic clock failure"), now]

    def clock() -> datetime:
        value = clock_values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value  # type: ignore[return-value]

    observer = WindowsOwnerPresenceObserver(
        audit,
        away_after_seconds=1,
        api=FakeLastInputApi(
            last_ticks=[1_000] * 6,
            current_ticks=[5_000, 6_000, 7_000],
        ),
        clock=clock,
    )

    assert observer.observe().presence is OwnerPresence.ACTIVE
    with pytest.raises(RuntimeError, match="synthetic clock failure"):
        observer.observe()
    assert observer.observe().presence is OwnerPresence.ACTIVE

    sampled = audit.list_for(
        entity_type="owner_presence_source",
        entity_id="win32-owner-presence",
    )
    assert len(sampled) == 2
    assert [event.payload["presence"] for event in sampled] == ["active", "active"]


def test_first_stable_ancient_tick_cannot_authorize_away(tmp_path: Path) -> None:
    observer, _audit_log = _observer(
        tmp_path,
        last_ticks=[1, 1],
        current_ticks=[1_000_000],
        away_after_seconds=1,
    )

    assert observer.observe().presence is OwnerPresence.ACTIVE


def test_changed_last_input_tick_resets_stability_probation(tmp_path: Path) -> None:
    observer, _audit_log = _observer(
        tmp_path,
        last_ticks=[1_000, 1_000, 2_000, 2_000, 2_000, 2_000, 2_000, 2_000],
        current_ticks=[5_000, 6_000, 6_500, 7_000],
        away_after_seconds=1,
    )

    assert observer.observe().presence is OwnerPresence.ACTIVE
    assert observer.observe().presence is OwnerPresence.ACTIVE
    assert observer.observe().presence is OwnerPresence.ACTIVE
    assert observer.observe().presence is OwnerPresence.AWAY


def test_tick_count64_regression_fails_closed_and_resets_probation(tmp_path: Path) -> None:
    observer, audit = _observer(
        tmp_path,
        last_ticks=[1_000] * 6,
        current_ticks=[10_000, 9_000, 12_000],
        away_after_seconds=1,
    )

    assert observer.observe().presence is OwnerPresence.ACTIVE
    with pytest.raises(ValueError, match="GetTickCount64 regressed"):
        observer.observe()
    assert observer.observe().presence is OwnerPresence.ACTIVE

    sampled = audit.list_for(
        entity_type="owner_presence_source",
        entity_id="win32-owner-presence",
    )
    assert len(sampled) == 2
    assert [event.payload["presence"] for event in sampled] == ["active", "active"]


def test_input_after_current_tick_sample_can_legitimately_be_ahead_and_is_active(
    tmp_path: Path,
) -> None:
    observer, _audit_log = _observer(
        tmp_path,
        last_ticks=[1_000, 6_000],
        current_ticks=[5_000],
        away_after_seconds=1,
    )

    assert observer.observe().presence is OwnerPresence.ACTIVE
