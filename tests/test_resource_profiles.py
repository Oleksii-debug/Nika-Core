from __future__ import annotations

from dataclasses import replace
from math import nan

import pytest

from nika_core.resource_profiles import (
    ResourceProfileName,
    ResourceProfilePolicy,
    WorkloadClass,
    budget_for_profile,
)
from nika_core.resources.contracts import ResourceSnapshot

_GIB = 1024 * 1024 * 1024
_HUGE_INT = 10**1000
_TOO_LARGE_SIGNED_INT = 1 << 63


def _snapshot(
    *,
    cpu: float = 20.0,
    memory: float = 30.0,
    available: int = 8 * _GIB,
) -> ResourceSnapshot:
    return ResourceSnapshot(
        cpu_percent=cpu,
        memory_percent=memory,
        available_memory_bytes=available,
    )


def test_normal_profile_allows_general_work_under_limits() -> None:
    decision = ResourceProfilePolicy().evaluate(
        profile=ResourceProfileName.NORMAL,
        snapshot=_snapshot(),
        requested_workload=WorkloadClass.GENERAL,
    )

    assert decision.allowed is True
    assert decision.reason == "profile_allows"
    assert decision.recommendations == ()


def test_heavy_workloads_are_mutually_exclusive_by_default() -> None:
    decision = ResourceProfilePolicy().evaluate(
        profile="normal",
        snapshot=_snapshot(),
        requested_workload="local_model",
        active_workloads=("transcription",),
    )

    assert decision.allowed is False
    assert decision.reason == "heavy_workload_conflict"


def test_low_memory_profile_blocks_heavy_work_and_recommends_only_safe_unload() -> None:
    decision = ResourceProfilePolicy().evaluate(
        profile="low_memory",
        snapshot=_snapshot(memory=50.0, available=4 * _GIB),
        requested_workload="local_model",
    )

    assert decision.allowed is False
    assert decision.reason == "profile_blocks_workload"
    assert decision.recommendations == ("unload_idle_local_model_if_safe",)


def test_economy_profile_blocks_cpu_pressure() -> None:
    decision = ResourceProfilePolicy().evaluate(
        profile="economy",
        snapshot=_snapshot(cpu=75.1),
        requested_workload="general",
    )

    assert decision.allowed is False
    assert decision.reason == "cpu_pressure"


def test_available_memory_floor_is_enforced() -> None:
    decision = ResourceProfilePolicy().evaluate(
        profile="normal",
        snapshot=_snapshot(available=(512 * 1024 * 1024) - 1),
        requested_workload="general",
    )

    assert decision.allowed is False
    assert decision.reason == "available_memory_floor"


@pytest.mark.parametrize(
    ("snapshot", "reason"),
    [
        (_snapshot(cpu=nan), "invalid_resource_snapshot"),
        (_snapshot(memory=101.0), "invalid_resource_snapshot"),
        (_snapshot(available=-1), "invalid_resource_snapshot"),
        (_snapshot(cpu=True), "invalid_resource_snapshot"),
        (_snapshot(memory=True), "invalid_resource_snapshot"),
        (_snapshot(available=True), "invalid_resource_snapshot"),
        (_snapshot(cpu=_HUGE_INT), "invalid_resource_snapshot"),
        (_snapshot(memory=_HUGE_INT), "invalid_resource_snapshot"),
        (_snapshot(available=_TOO_LARGE_SIGNED_INT), "invalid_resource_snapshot"),
    ],
)
def test_invalid_telemetry_fails_closed(snapshot: ResourceSnapshot, reason: str) -> None:
    decision = ResourceProfilePolicy().evaluate(
        profile="normal",
        snapshot=snapshot,
        requested_workload="general",
    )

    assert decision.allowed is False
    assert decision.reason == reason


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("max_cpu_percent", True),
        ("max_memory_percent", True),
        ("min_available_memory_bytes", True),
        ("max_simultaneous_heavy_workloads", True),
        ("max_cpu_percent", _HUGE_INT),
        ("max_memory_percent", _HUGE_INT),
        ("min_available_memory_bytes", _TOO_LARGE_SIGNED_INT),
        ("max_simultaneous_heavy_workloads", _TOO_LARGE_SIGNED_INT),
    ],
)
def test_malformed_or_unbounded_profile_limits_are_rejected(
    field_name: str, value: object
) -> None:
    baseline = ResourceProfilePolicy().profile_spec(ResourceProfileName.NORMAL)
    malformed = replace(baseline, **{field_name: value})

    with pytest.raises(ValueError):
        ResourceProfilePolicy({ResourceProfileName.NORMAL: malformed})


@pytest.mark.parametrize(
    "allowed_workloads",
    [
        {WorkloadClass.GENERAL},
        frozenset({"general"}),
    ],
)
def test_profile_workload_authority_requires_exact_immutable_enum_set(
    allowed_workloads: object,
) -> None:
    baseline = ResourceProfilePolicy().profile_spec(ResourceProfileName.NORMAL)
    malformed = replace(baseline, allowed_workloads=allowed_workloads)

    with pytest.raises(ValueError):
        ResourceProfilePolicy({ResourceProfileName.NORMAL: malformed})


def test_accepted_profile_workload_authority_cannot_be_mutated() -> None:
    policy = ResourceProfilePolicy()
    allowed = policy.profile_spec(ResourceProfileName.NORMAL).allowed_workloads

    assert type(allowed) is frozenset
    with pytest.raises(AttributeError):
        allowed.add(WorkloadClass.LOCAL_MODEL)  # type: ignore[attr-defined]


def test_profile_recommendation_flag_requires_boolean() -> None:
    baseline = ResourceProfilePolicy().profile_spec(ResourceProfileName.NORMAL)
    malformed = replace(baseline, recommend_idle_model_unload=1)

    with pytest.raises(ValueError):
        ResourceProfilePolicy({ResourceProfileName.NORMAL: malformed})


def test_unknown_profile_and_workload_fail_closed() -> None:
    policy = ResourceProfilePolicy()

    unknown_profile = policy.evaluate(
        profile="turbo",
        snapshot=_snapshot(),
        requested_workload="general",
    )
    unknown_workload = policy.evaluate(
        profile="normal",
        snapshot=_snapshot(),
        requested_workload="mystery",
    )
    unknown_active = policy.evaluate(
        profile="normal",
        snapshot=_snapshot(),
        requested_workload="general",
        active_workloads=("mystery",),
    )

    assert (unknown_profile.allowed, unknown_profile.reason) == (False, "unknown_profile")
    assert (unknown_workload.allowed, unknown_workload.reason) == (False, "unknown_workload")
    assert (unknown_active.allowed, unknown_active.reason) == (
        False,
        "unknown_active_workload",
    )


def test_profile_projects_to_existing_resource_budget_contract() -> None:
    budget = budget_for_profile(
        profile="economy",
        scope="workspace",
        owner_id="project-1",
        max_concurrent=2,
    )

    assert budget.scope == "workspace"
    assert budget.owner_id == "project-1"
    assert budget.max_concurrent == 2
    assert budget.max_cpu_percent == 75.0
    assert budget.max_memory_percent == 80.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"scope": "", "owner_id": "project-1", "max_concurrent": 1},
        {"scope": "workspace", "owner_id": " ", "max_concurrent": 1},
        {"scope": "workspace", "owner_id": "project-1", "max_concurrent": 0},
        {"scope": "workspace", "owner_id": "project-1", "max_concurrent": True},
        {"scope": 1, "owner_id": "project-1", "max_concurrent": 1},
        {"scope": "workspace", "owner_id": 1, "max_concurrent": 1},
    ],
)
def test_budget_adapter_rejects_invalid_identity_or_concurrency(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        budget_for_profile(profile="normal", **kwargs)  # type: ignore[arg-type]
