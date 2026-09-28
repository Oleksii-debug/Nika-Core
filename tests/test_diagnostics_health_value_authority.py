from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from enum import StrEnum

import pytest

from nika_core.diagnostics import (
    HealthCheck,
    HealthReport,
    HealthService,
    HealthStatus,
    ModelHealthFact,
    ModelHealthSnapshot,
)

_NOW = datetime(2026, 9, 14, 19, 30, tzinfo=UTC)


class _ForeignModelHealthFact(StrEnum):
    UNKNOWN = "unknown"


class _ForeignHealthStatus(StrEnum):
    PASS = "pass"


class _HealthCheckSubclass(HealthCheck):
    pass


class _HealthTextSubclass(str):
    pass


class _BehavioralDatetime(datetime):
    def astimezone(self, *args: object, **kwargs: object) -> datetime:
        raise AssertionError("behavioral datetime must not execute")


class _CheckTuple(tuple[HealthCheck, ...]):
    pass


def _snapshot_values() -> dict[str, object]:
    return {
        "configured": ModelHealthFact.YES,
        "reachable": ModelHealthFact.UNKNOWN,
        "model_present": ModelHealthFact.UNKNOWN,
        "model_ready": ModelHealthFact.UNKNOWN,
        "inference_proven": ModelHealthFact.UNKNOWN,
    }


@pytest.mark.parametrize(
    "field_name",
    ["configured", "reachable", "model_present", "model_ready", "inference_proven"],
)
@pytest.mark.parametrize("value", ["unknown", _ForeignModelHealthFact.UNKNOWN])
def test_model_health_snapshot_rejects_noncanonical_fact_values(
    field_name: str,
    value: object,
) -> None:
    values = _snapshot_values()
    values[field_name] = value

    with pytest.raises(TypeError, match=f"{field_name} must be a canonical ModelHealthFact"):
        ModelHealthSnapshot(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("field_name", ["check_id", "summary"])
@pytest.mark.parametrize("value", [[], _HealthTextSubclass("noncanonical")])
def test_health_check_rejects_noncanonical_text_carriers(
    field_name: str,
    value: object,
) -> None:
    values: dict[str, object] = {
        "check_id": "authority",
        "status": HealthStatus.PASS,
        "summary": "canonical",
    }
    values[field_name] = value

    with pytest.raises(TypeError, match=f"{field_name} must be canonical text"):
        HealthCheck(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("field_name", ["check_id", "summary"])
def test_health_report_revalidates_constructor_bypassed_text_carriers(
    field_name: str,
) -> None:
    forged = object.__new__(HealthCheck)
    object.__setattr__(forged, "check_id", "authority")
    object.__setattr__(forged, "status", HealthStatus.PASS)
    object.__setattr__(forged, "summary", "canonical")
    object.__setattr__(forged, field_name, [])

    with pytest.raises(TypeError, match=f"{field_name} must be canonical text"):
        HealthReport(generated_at=_NOW, checks=(forged,))


@pytest.mark.parametrize("status", ["pass", _ForeignHealthStatus.PASS])
def test_health_check_rejects_noncanonical_status(status: object) -> None:
    with pytest.raises(TypeError, match="status must be a canonical HealthStatus"):
        HealthCheck(
            check_id="authority",
            status=status,  # type: ignore[arg-type]
            summary="canonical status required",
        )


@pytest.mark.parametrize(
    "value",
    ["2026-09-27T21:00:00+00:00", _BehavioralDatetime(2026, 9, 27, tzinfo=UTC)],
)
def test_health_report_rejects_noncanonical_timestamp_carriers(value: object) -> None:
    with pytest.raises(TypeError, match="generated_at must be a canonical datetime"):
        HealthReport(generated_at=value, checks=())  # type: ignore[arg-type]


def test_health_report_rejects_naive_timestamp() -> None:
    naive = datetime(2026, 9, 27, 21, 0, tzinfo=UTC).replace(tzinfo=None)
    with pytest.raises(ValueError, match="generated_at must be timezone-aware"):
        HealthReport(generated_at=naive, checks=())


def test_health_report_snapshots_timestamp_in_canonical_utc() -> None:
    local = datetime(
        2026,
        9,
        27,
        23,
        0,
        tzinfo=timezone(timedelta(hours=2)),
    )
    report = HealthReport(generated_at=local, checks=())

    assert report.generated_at == datetime(2026, 9, 27, 21, 0, tzinfo=UTC)
    assert report.generated_at.tzinfo is UTC
    assert report.as_dict()["generated_at"] == "2026-09-27T21:00:00+00:00"


def test_health_service_rejects_behavioral_clock_before_datetime_methods() -> None:
    service = object.__new__(HealthService)
    service._clock = lambda: _BehavioralDatetime(2026, 9, 27, tzinfo=UTC)

    with pytest.raises(TypeError, match="health clock must return a canonical datetime"):
        service._normalized_now()


def test_health_report_rejects_mutable_check_container() -> None:
    check = HealthCheck(
        check_id="authority",
        status=HealthStatus.PASS,
        summary="canonical",
    )

    with pytest.raises(TypeError, match="checks must be an immutable tuple"):
        HealthReport(generated_at=_NOW, checks=[check])  # type: ignore[arg-type]


def test_health_report_rejects_tuple_subclass_container() -> None:
    check = HealthCheck(
        check_id="authority",
        status=HealthStatus.PASS,
        summary="canonical",
    )

    with pytest.raises(TypeError, match="checks must be an immutable tuple"):
        HealthReport(
            generated_at=_NOW,
            checks=_CheckTuple((check,)),  # type: ignore[arg-type]
        )


def test_health_report_rejects_noncanonical_check_carrier() -> None:
    check = _HealthCheckSubclass(
        check_id="authority",
        status=HealthStatus.PASS,
        summary="canonical",
    )

    with pytest.raises(TypeError, match="checks must contain canonical HealthCheck values"):
        HealthReport(generated_at=_NOW, checks=(check,))


def test_health_report_revalidates_constructor_bypassed_status() -> None:
    forged = object.__new__(HealthCheck)
    object.__setattr__(forged, "check_id", "authority")
    object.__setattr__(forged, "status", "pass")
    object.__setattr__(forged, "summary", "forged")

    with pytest.raises(TypeError, match="status must be a canonical HealthStatus"):
        HealthReport(generated_at=_NOW, checks=(forged,))


def test_health_report_snapshots_canonical_checks() -> None:
    check = HealthCheck(
        check_id="authority",
        status=HealthStatus.PASS,
        summary="canonical",
    )
    report = HealthReport(generated_at=_NOW, checks=(check,))

    object.__setattr__(check, "status", HealthStatus.FAIL)

    assert report.checks[0] is not check
    assert report.overall is HealthStatus.PASS
    assert report.as_dict()["checks"] == [
        {
            "check_id": "authority",
            "status": "pass",
            "summary": "canonical",
        }
    ]
