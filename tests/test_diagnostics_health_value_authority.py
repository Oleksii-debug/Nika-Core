from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

import pytest

from nika_core.diagnostics import (
    HealthCheck,
    HealthReport,
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


@pytest.mark.parametrize("status", ["pass", _ForeignHealthStatus.PASS])
def test_health_check_rejects_noncanonical_status(status: object) -> None:
    with pytest.raises(TypeError, match="status must be a canonical HealthStatus"):
        HealthCheck(
            check_id="authority",
            status=status,  # type: ignore[arg-type]
            summary="canonical status required",
        )


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
