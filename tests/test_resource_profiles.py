from __future__ import annotations

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
    ],
)
def test_budget_adapter_rejects_invalid_identity_or_concurrency(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        budget_for_profile(profile="normal", **kwargs)  # type: ignore[arg-type]
