from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone, tzinfo
from enum import StrEnum
from pathlib import Path

import pytest

from nika_core.config import AppConfig
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


class _BehavioralTimezone(tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        raise AssertionError("behavioral timezone must not execute")

    def dst(self, dt: datetime | None) -> timedelta | None:
        raise AssertionError("behavioral timezone must not execute")

    def tzname(self, dt: datetime | None) -> str | None:
        raise AssertionError("behavioral timezone must not execute")


class _BehavioralInt(int):
    def __eq__(self, other: object) -> bool:
        raise AssertionError("behavioral int must not execute")


class _BehavioralString(str):
    def strip(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("behavioral string must not execute")


class _BehavioralPath:
    def __fspath__(self) -> str:
        raise AssertionError("behavioral path must not execute")


class _BehavioralClock:
    def __bool__(self) -> bool:
        raise AssertionError("clock truthiness must not execute")

    def __call__(self) -> datetime:
        return _NOW


class _AppConfigSubclass(AppConfig):
    pass


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


def test_health_report_rejects_behavioral_timezone_before_offset_methods() -> None:
    value = datetime(2026, 9, 27, 21, 0, tzinfo=_BehavioralTimezone())

    with pytest.raises(TypeError, match="generated_at timezone must be canonical"):
        HealthReport(generated_at=value, checks=())


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


def test_health_service_rejects_behavioral_clock_timezone_before_offset_methods() -> None:
    service = object.__new__(HealthService)
    service._clock = lambda: datetime(
        2026,
        9,
        27,
        21,
        0,
        tzinfo=_BehavioralTimezone(),
    )

    with pytest.raises(TypeError, match="health clock timezone must be canonical"):
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


def test_health_service_does_not_evaluate_injected_clock_truthiness(
    tmp_path: Path,
) -> None:
    service = HealthService(
        AppConfig(database_path=tmp_path / "nika.db"),
        clock=_BehavioralClock(),
    )

    assert service._normalized_now() == _NOW


def test_health_service_rejects_app_config_subclass(tmp_path: Path) -> None:
    config = _AppConfigSubclass(database_path=tmp_path / "nika.db")

    with pytest.raises(TypeError, match="config must be a canonical AppConfig"):
        HealthService(config)


@pytest.mark.parametrize(
    ("field_name", "value", "message"),
    [
        ("schema_version", _BehavioralInt(1), "schema_version must be a canonical int"),
        ("app_version", _BehavioralString("0.0.2"), "app_version must be canonical text"),
        (
            "database_path",
            _BehavioralPath(),
            "database_path must be a canonical platform Path",
        ),
    ],
)
def test_health_service_rejects_forged_config_field_carriers_before_behavior(
    tmp_path: Path,
    field_name: str,
    value: object,
    message: str,
) -> None:
    config = AppConfig(database_path=tmp_path / "nika.db")
    object.__setattr__(config, field_name, value)

    with pytest.raises(TypeError, match=message):
        HealthService(config)


def test_health_service_snapshots_config_authority(tmp_path: Path) -> None:
    original_path = tmp_path / "original.db"
    config = AppConfig(database_path=original_path)
    service = HealthService(config)

    config.schema_version = 999
    config.app_version = ""
    config.database_path = tmp_path / "replacement.db"

    check = service._check_configuration()

    assert check.status is HealthStatus.PASS
    assert service._database_path == original_path
