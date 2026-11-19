import pytest

from nika_core.background_life import (
    BackgroundAction,
    BackgroundWorkKind,
    OwnerPresence,
    decide_background_work,
)
from nika_core.resources.contracts import (
    ResourceBudget,
    ResourceCapacityStatus,
    ResourceSnapshot,
)


def capacity(
    *,
    scope: str = "background_life",
    max_concurrent: int = 1,
    active_count: int = 0,
    queued_count: int = 0,
    cpu_percent: float = 10.0,
    memory_percent: float = 20.0,
    max_cpu_percent: float | None = 80.0,
    max_memory_percent: float | None = 80.0,
    power_plugged: bool | None = True,
) -> ResourceCapacityStatus:
    budget = ResourceBudget(
        scope=scope,
        owner_id="living-agent",
        max_concurrent=max_concurrent,
        max_cpu_percent=max_cpu_percent,
        max_memory_percent=max_memory_percent,
    )
    snapshot = ResourceSnapshot(
        cpu_percent=cpu_percent,
        memory_percent=memory_percent,
        available_memory_bytes=8 * 1024 * 1024 * 1024,
        power_plugged=power_plugged,
    )
    reasons: list[str] = []
    if active_count >= max_concurrent:
        reasons.append("concurrency_limit")
    if max_cpu_percent is not None and cpu_percent > max_cpu_percent:
        reasons.append("cpu_limit")
    if max_memory_percent is not None and memory_percent > max_memory_percent:
        reasons.append("memory_limit")
    return ResourceCapacityStatus(
        budget=budget,
        snapshot=snapshot,
        active_count=active_count,
        queued_count=queued_count,
        concurrency_headroom=max(0, max_concurrent - active_count),
        cpu_headroom_percent=(
            None if max_cpu_percent is None else max_cpu_percent - cpu_percent
        ),
        memory_headroom_percent=(
            None if max_memory_percent is None else max_memory_percent - memory_percent
        ),
        pressure_reasons=tuple(reasons),
    )


@pytest.mark.parametrize("work_kind", list(BackgroundWorkKind))
def test_owner_away_with_capacity_runs_each_binding_activity(work_kind):
    result = decide_background_work(
        owner_presence=OwnerPresence.AWAY,
        work_kind=work_kind,
        capacity=capacity(),
    )

    assert result.action is BackgroundAction.RUN
    assert result.allowed is True
    assert result.reason == "owner_away_capacity_available"


@pytest.mark.parametrize(
    ("presence", "reason"),
    [
        (OwnerPresence.ACTIVE, "owner_active"),
        (OwnerPresence.UNKNOWN, "owner_presence_unknown"),
    ],
)
def test_owner_presence_pauses_before_resource_or_power_decisions(presence, reason):
    result = decide_background_work(
        owner_presence=presence,
        work_kind=BackgroundWorkKind.BOUNDED_ML_PILOT,
        capacity=capacity(
            active_count=1,
            cpu_percent=90.0,
            memory_percent=90.0,
            power_plugged=False,
        ),
    )

    assert result.action is BackgroundAction.PAUSE
    assert result.allowed is False
    assert result.reason == reason


@pytest.mark.parametrize(
    ("presence", "reason"),
    [
        (OwnerPresence.ACTIVE, "owner_active"),
        (OwnerPresence.UNKNOWN, "owner_presence_unknown"),
    ],
)
def test_owner_presence_pause_does_not_depend_on_capacity_evidence(presence, reason):
    result = decide_background_work(
        owner_presence=presence,
        work_kind=BackgroundWorkKind.SELF_TEST,
        capacity=None,  # type: ignore[arg-type]
    )

    assert result.action is BackgroundAction.PAUSE
    assert result.reason == reason


@pytest.mark.parametrize(
    "kwargs",
    [
        {"active_count": 1},
        {"cpu_percent": 90.0},
        {"memory_percent": 90.0},
    ],
)
def test_canonical_resource_pressure_defers_owner_away_work(kwargs):
    result = decide_background_work(
        owner_presence=OwnerPresence.AWAY,
        work_kind=BackgroundWorkKind.READING_RESEARCH,
        capacity=capacity(**kwargs),
    )

    assert result.action is BackgroundAction.DEFER
    assert result.reason == "resource_pressure"


@pytest.mark.parametrize(
    ("power_plugged", "reason"),
    [
        (False, "battery_power"),
        (None, "power_state_unknown"),
    ],
)
@pytest.mark.parametrize(
    "work_kind",
    [
        BackgroundWorkKind.LOCAL_MODEL_DEBATE,
        BackgroundWorkKind.BOUNDED_ML_PILOT,
        BackgroundWorkKind.EVALUATION,
    ],
)
def test_high_impact_background_work_requires_known_ac_power(
    power_plugged,
    reason,
    work_kind,
):
    result = decide_background_work(
        owner_presence=OwnerPresence.AWAY,
        work_kind=work_kind,
        capacity=capacity(power_plugged=power_plugged),
    )

    assert result.action is BackgroundAction.DEFER
    assert result.reason == reason


def test_low_impact_work_can_run_on_battery_when_capacity_is_available():
    result = decide_background_work(
        owner_presence=OwnerPresence.AWAY,
        work_kind=BackgroundWorkKind.MEMORY_CONSOLIDATION,
        capacity=capacity(power_plugged=False),
    )

    assert result.action is BackgroundAction.RUN


def test_capacity_must_be_scoped_to_background_life():
    with pytest.raises(ValueError, match="background_life"):
        decide_background_work(
            owner_presence=OwnerPresence.AWAY,
            work_kind=BackgroundWorkKind.SELF_TEST,
            capacity=capacity(scope="model_training"),
        )


def test_wrong_presence_or_work_kind_type_fails_closed():
    with pytest.raises(TypeError, match="owner_presence"):
        decide_background_work(
            owner_presence="away",  # type: ignore[arg-type]
            work_kind=BackgroundWorkKind.SELF_TEST,
            capacity=capacity(),
        )

    with pytest.raises(TypeError, match="work_kind"):
        decide_background_work(
            owner_presence=OwnerPresence.AWAY,
            work_kind="self_test",  # type: ignore[arg-type]
            capacity=capacity(),
        )


class ForgedCapacity(ResourceCapacityStatus):
    pass


def test_capacity_subclass_cannot_spoof_canonical_resource_authority():
    base = capacity()
    forged = ForgedCapacity(
        budget=base.budget,
        snapshot=base.snapshot,
        active_count=base.active_count,
        queued_count=base.queued_count,
        concurrency_headroom=base.concurrency_headroom,
        cpu_headroom_percent=base.cpu_headroom_percent,
        memory_headroom_percent=base.memory_headroom_percent,
        pressure_reasons=base.pressure_reasons,
    )

    with pytest.raises(TypeError, match="exact ResourceCapacityStatus"):
        decide_background_work(
            owner_presence=OwnerPresence.AWAY,
            work_kind=BackgroundWorkKind.SELF_TEST,
            capacity=forged,
        )


def test_exact_dataclass_cannot_forge_concurrency_capacity():
    status = capacity(active_count=1)
    object.__setattr__(status, "concurrency_headroom", 1)
    object.__setattr__(status, "pressure_reasons", ())

    with pytest.raises(ValueError, match="concurrency_headroom"):
        decide_background_work(
            owner_presence=OwnerPresence.AWAY,
            work_kind=BackgroundWorkKind.SELF_TEST,
            capacity=status,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pressure_reasons", ()),
        ("cpu_headroom_percent", 70.0),
    ],
)
def test_exact_dataclass_cannot_hide_cpu_pressure(field, value):
    status = capacity(cpu_percent=90.0)
    object.__setattr__(status, field, value)

    with pytest.raises(ValueError, match="inconsistent"):
        decide_background_work(
            owner_presence=OwnerPresence.AWAY,
            work_kind=BackgroundWorkKind.SELF_TEST,
            capacity=status,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pressure_reasons", ()),
        ("memory_headroom_percent", 60.0),
    ],
)
def test_exact_dataclass_cannot_hide_memory_pressure(field, value):
    status = capacity(memory_percent=90.0)
    object.__setattr__(status, field, value)

    with pytest.raises(ValueError, match="inconsistent"):
        decide_background_work(
            owner_presence=OwnerPresence.AWAY,
            work_kind=BackgroundWorkKind.SELF_TEST,
            capacity=status,
        )


@pytest.mark.parametrize(
    ("target", "field", "value"),
    [
        ("budget", "max_concurrent", True),
        ("budget", "max_cpu_percent", True),
        ("budget", "max_memory_percent", 0.0),
        ("snapshot", "cpu_percent", True),
        ("snapshot", "memory_percent", float("nan")),
        ("snapshot", "available_memory_bytes", True),
    ],
)
def test_malformed_exact_budget_or_snapshot_fails_closed(target, field, value):
    status = capacity()
    nested = getattr(status, target)
    object.__setattr__(nested, field, value)

    with pytest.raises((TypeError, ValueError)):
        decide_background_work(
            owner_presence=OwnerPresence.AWAY,
            work_kind=BackgroundWorkKind.SELF_TEST,
            capacity=status,
        )


def test_malformed_capacity_counts_fail_closed():
    status = capacity()
    object.__setattr__(status, "concurrency_headroom", True)

    with pytest.raises(TypeError, match="exact built-in int"):
        decide_background_work(
            owner_presence=OwnerPresence.AWAY,
            work_kind=BackgroundWorkKind.SELF_TEST,
            capacity=status,
        )


def test_noncanonical_pressure_reason_order_or_name_fails_closed():
    status = capacity(active_count=1, cpu_percent=90.0)
    object.__setattr__(status, "pressure_reasons", ("cpu_limit", "concurrency_limit"))

    with pytest.raises(ValueError, match="pressure_reasons"):
        decide_background_work(
            owner_presence=OwnerPresence.AWAY,
            work_kind=BackgroundWorkKind.SELF_TEST,
            capacity=status,
        )


def test_policy_has_no_scheduler_or_effect_methods():
    result = decide_background_work(
        owner_presence=OwnerPresence.AWAY,
        work_kind=BackgroundWorkKind.UNFINISHED_WORK,
        capacity=capacity(),
    )

    assert not hasattr(result, "schedule")
    assert not hasattr(result, "execute")
    assert not hasattr(result, "resume")
