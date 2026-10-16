from __future__ import annotations

from types import SimpleNamespace

import pytest

from nika_core.resources import psutil_adapter
from nika_core.resources.psutil_adapter import PsutilResourceObserver


def _host(
    monkeypatch, *, battery, cores=6, cpu=25.0, memory=40.0, available=6_000, total=10_000
) -> None:
    monkeypatch.setattr(psutil_adapter.psutil, "cpu_percent", lambda *, interval: cpu)
    monkeypatch.setattr(psutil_adapter.psutil, "cpu_count", lambda *, logical: cores)
    monkeypatch.setattr(
        psutil_adapter.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(percent=memory, available=available, total=total),
    )
    monkeypatch.setattr(psutil_adapter.psutil, "sensors_battery", lambda: battery)


@pytest.mark.parametrize(
    ("power", "expected"),
    [(True, True), (False, False), (None, None), (1, None), (0, None), ("yes", None)],
)
def test_optional_power_does_not_invent_battery_state(monkeypatch, power, expected) -> None:
    _host(monkeypatch, battery=SimpleNamespace(percent=55, power_plugged=power))
    process = SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=1200))
    snapshot = PsutilResourceObserver(process).snapshot()
    assert snapshot.power_plugged is expected
    assert snapshot.battery_percent == 55.0
    assert snapshot.process_rss_bytes == 1200
    assert snapshot.cpu_percent == 25.0


@pytest.mark.parametrize("percent", [None, -1, 101, float("nan"), float("inf"), True, 10**1000])
def test_invalid_optional_battery_percentage_is_unknown(monkeypatch, percent) -> None:
    _host(monkeypatch, battery=SimpleNamespace(percent=percent, power_plugged=True))
    process = SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=1200))
    snapshot = PsutilResourceObserver(process).snapshot()
    assert snapshot.battery_percent is None
    assert snapshot.power_plugged is True


@pytest.mark.parametrize("rss", [-1, 3.5, True, "100", None, 1000])
def test_optional_rss_rejects_malformed_values(monkeypatch, rss) -> None:
    _host(monkeypatch, battery=None)
    process = SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=rss))
    assert PsutilResourceObserver(process).snapshot().process_rss_bytes == (
        rss if type(rss) is int and rss >= 0 else None
    )


@pytest.mark.parametrize("cores", [None, 0, -1, True, 2.5, "6", 8])
def test_optional_cpu_count_rejects_malformed_values(monkeypatch, cores) -> None:
    _host(monkeypatch, battery=None, cores=cores)
    process = SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=100))
    assert PsutilResourceObserver(process).snapshot().logical_cpu_count == (
        cores if type(cores) is int and cores > 0 else None
    )


def test_missing_optional_telemetry_does_not_break_required_cpu_memory(monkeypatch) -> None:
    _host(monkeypatch, battery=None)

    class UnreadableProcess:
        def memory_info(self):
            raise OSError("private path not available")

    snapshot = PsutilResourceObserver(UnreadableProcess()).snapshot()
    assert snapshot.process_rss_bytes is None
    assert snapshot.battery_percent is None
    assert snapshot.power_plugged is None
    assert (snapshot.cpu_percent, snapshot.memory_percent) == (25.0, 40.0)


@pytest.mark.parametrize(
    "reading", [True, False, "25", -1, 101, float("nan"), float("inf"), 10**1000]
)
@pytest.mark.parametrize("field", ["cpu", "memory"])
def test_malformed_required_percent_never_becomes_valid_capacity(
    monkeypatch, reading, field
) -> None:
    options = {field: reading}
    _host(monkeypatch, battery=None, **options)
    process = SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=100))
    with pytest.raises(ValueError, match="must be a finite percentage") as exc:
        PsutilResourceObserver(process).snapshot()
    assert repr(reading) not in str(exc.value)


@pytest.mark.parametrize("available", [True, False, -1, 6.5, "6000", None, float("nan")])
def test_malformed_required_available_memory_fails_closed(monkeypatch, available) -> None:
    _host(monkeypatch, battery=None, available=available)
    process = SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=100))
    with pytest.raises(ValueError, match="Host available memory must be a nonnegative integer"):
        PsutilResourceObserver(process).snapshot()


@pytest.mark.parametrize("total", [None, -1, 5.5, True, "10000", 10_000])
def test_invalid_optional_total_memory_is_unavailable(monkeypatch, total) -> None:
    _host(monkeypatch, battery=None, total=total)
    process = SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=100))
    assert PsutilResourceObserver(process).snapshot().total_memory_bytes == (
        total if type(total) is int and total >= 0 else None
    )


def test_optional_cpu_count_exception_does_not_hide_valid_host_capacity(monkeypatch) -> None:
    _host(monkeypatch, battery=None)

    def unsupported_cpu_count(*, logical):
        raise OSError("private CPU topology unavailable")

    monkeypatch.setattr(psutil_adapter.psutil, "cpu_count", unsupported_cpu_count)
    process = SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=100))
    snapshot = PsutilResourceObserver(process).snapshot()
    assert snapshot.logical_cpu_count is None
    assert (snapshot.cpu_percent, snapshot.memory_percent) == (25.0, 40.0)
