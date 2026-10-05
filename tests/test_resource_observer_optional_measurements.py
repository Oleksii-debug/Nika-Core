from __future__ import annotations

from types import SimpleNamespace

import pytest

from nika_core.resources import psutil_adapter
from nika_core.resources.psutil_adapter import PsutilResourceObserver


def _host(monkeypatch, *, battery, cores=6) -> None:
    monkeypatch.setattr(psutil_adapter.psutil, "cpu_percent", lambda *, interval: 25.0)
    monkeypatch.setattr(psutil_adapter.psutil, "cpu_count", lambda *, logical: cores)
    monkeypatch.setattr(
        psutil_adapter.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(percent=40.0, available=6_000, total=10_000),
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
