from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite

from nika_core.resources.contracts import (
    ResourceBudget,
    ResourceCapacityStatus,
    ResourceSnapshot,
)

_BACKGROUND_SCOPE = "background_life"
_MAX_SIGNED_64 = (1 << 63) - 1


class OwnerPresence(StrEnum):
    """Externally observed owner-availability state."""

    ACTIVE = "active"
    AWAY = "away"
    UNKNOWN = "unknown"


class BackgroundWorkKind(StrEnum):
    """Binding OWNER_AWAY activity classes from the learning runtime vision."""

    UNFINISHED_WORK = "unfinished_work"
    READING_RESEARCH = "reading_research"
    SELF_TEST = "self_test"
    LOCAL_MODEL_DEBATE = "local_model_debate"
    EVIDENCE_VERIFICATION = "evidence_verification"
    MEMORY_CONSOLIDATION = "memory_consolidation"
    KNOWLEDGE_RECONCILIATION = "knowledge_reconciliation"
    CANDIDATE_PREPARATION = "candidate_preparation"
    BOUNDED_ML_PILOT = "bounded_ml_pilot"
    EVALUATION = "evaluation"


class BackgroundAction(StrEnum):
    """A recommendation only; this module never schedules or executes work."""

    RUN = "run"
    PAUSE = "pause"
    DEFER = "defer"


_HIGH_IMPACT_WORK = frozenset(
    {
        BackgroundWorkKind.LOCAL_MODEL_DEBATE,
        BackgroundWorkKind.BOUNDED_ML_PILOT,
        BackgroundWorkKind.EVALUATION,
    }
)


@dataclass(frozen=True, slots=True)
class BackgroundDecision:
    """Text-first admission result with no scheduler/effect authority."""

    action: BackgroundAction
    work_kind: BackgroundWorkKind
    reason: str
    capacity_scope: str = _BACKGROUND_SCOPE

    @property
    def allowed(self) -> bool:
        return self.action is BackgroundAction.RUN


def _require_nonempty_exact_str(value: object, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be exact built-in str")
    if not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty without surrounding whitespace")
    return value


def _require_bounded_int(value: object, name: str, *, minimum: int) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be exact built-in int")
    if not minimum <= value <= _MAX_SIGNED_64:
        raise ValueError(f"{name} is outside the supported integer range")
    return value


def _require_percent(
    value: object,
    name: str,
    *,
    allow_none: bool,
    lower_inclusive: bool,
) -> int | float | None:
    if value is None:
        if allow_none:
            return None
        raise TypeError(f"{name} must be a finite number")
    if type(value) not in (int, float):
        raise TypeError(f"{name} must be exact built-in int or float")
    if type(value) is float and not isfinite(value):
        raise ValueError(f"{name} must be finite")
    lower_ok = value >= 0 if lower_inclusive else value > 0
    if not lower_ok or value > 100:
        bracket = "[0, 100]" if lower_inclusive else "(0, 100]"
        raise ValueError(f"{name} must be in {bracket}")
    return value


def _require_headroom(
    value: object,
    expected: int | float | None,
    name: str,
) -> None:
    if expected is None:
        if value is not None:
            raise ValueError(f"{name} must be None when no limit is configured")
        return
    if type(value) not in (int, float):
        raise TypeError(f"{name} must be exact built-in int or float")
    if type(value) is float and not isfinite(value):
        raise ValueError(f"{name} must be finite")
    if value != expected:
        raise ValueError(f"{name} is inconsistent with canonical ResourceManager status")


def _validate_capacity(capacity: object) -> ResourceCapacityStatus:
    if type(capacity) is not ResourceCapacityStatus:
        raise TypeError("capacity must be exact ResourceCapacityStatus")
    if type(capacity.budget) is not ResourceBudget:
        raise TypeError("capacity.budget must be exact ResourceBudget")
    if type(capacity.snapshot) is not ResourceSnapshot:
        raise TypeError("capacity.snapshot must be exact ResourceSnapshot")

    budget = capacity.budget
    snapshot = capacity.snapshot

    scope = _require_nonempty_exact_str(budget.scope, "capacity budget scope")
    if scope != _BACKGROUND_SCOPE:
        raise ValueError(f"capacity budget scope must be {_BACKGROUND_SCOPE!r}")
    _require_nonempty_exact_str(budget.owner_id, "capacity budget owner_id")
    max_concurrent = _require_bounded_int(
        budget.max_concurrent,
        "capacity budget max_concurrent",
        minimum=1,
    )
    max_cpu = _require_percent(
        budget.max_cpu_percent,
        "capacity budget max_cpu_percent",
        allow_none=True,
        lower_inclusive=False,
    )
    max_memory = _require_percent(
        budget.max_memory_percent,
        "capacity budget max_memory_percent",
        allow_none=True,
        lower_inclusive=False,
    )
    cpu = _require_percent(
        snapshot.cpu_percent,
        "capacity snapshot cpu_percent",
        allow_none=False,
        lower_inclusive=True,
    )
    memory = _require_percent(
        snapshot.memory_percent,
        "capacity snapshot memory_percent",
        allow_none=False,
        lower_inclusive=True,
    )
    _require_bounded_int(
        snapshot.available_memory_bytes,
        "capacity snapshot available_memory_bytes",
        minimum=0,
    )
    if snapshot.power_plugged is not None and type(snapshot.power_plugged) is not bool:
        raise TypeError("power_plugged must be exact built-in bool or None")

    active_count = _require_bounded_int(capacity.active_count, "active_count", minimum=0)
    _require_bounded_int(capacity.queued_count, "queued_count", minimum=0)
    concurrency_headroom = _require_bounded_int(
        capacity.concurrency_headroom,
        "concurrency_headroom",
        minimum=0,
    )
    expected_concurrency_headroom = max(0, max_concurrent - active_count)
    if concurrency_headroom != expected_concurrency_headroom:
        raise ValueError(
            "concurrency_headroom is inconsistent with canonical ResourceManager status"
        )

    expected_cpu_headroom = None if max_cpu is None else max_cpu - cpu
    expected_memory_headroom = None if max_memory is None else max_memory - memory
    _require_headroom(
        capacity.cpu_headroom_percent,
        expected_cpu_headroom,
        "cpu_headroom_percent",
    )
    _require_headroom(
        capacity.memory_headroom_percent,
        expected_memory_headroom,
        "memory_headroom_percent",
    )

    expected_reasons: list[str] = []
    if active_count >= max_concurrent:
        expected_reasons.append("concurrency_limit")
    if max_cpu is not None and cpu > max_cpu:
        expected_reasons.append("cpu_limit")
    if max_memory is not None and memory > max_memory:
        expected_reasons.append("memory_limit")

    if type(capacity.pressure_reasons) is not tuple:
        raise TypeError("pressure_reasons must be exact tuple")
    if capacity.pressure_reasons != tuple(expected_reasons):
        raise ValueError(
            "pressure_reasons are inconsistent with canonical ResourceManager status"
        )
    return capacity


def decide_background_work(
    *,
    owner_presence: OwnerPresence,
    work_kind: BackgroundWorkKind,
    capacity: ResourceCapacityStatus,
) -> BackgroundDecision:
    """Decide whether OWNER_AWAY work may run without causing any effect."""

    if type(owner_presence) is not OwnerPresence:
        raise TypeError("owner_presence must be OwnerPresence")
    if type(work_kind) is not BackgroundWorkKind:
        raise TypeError("work_kind must be BackgroundWorkKind")

    if owner_presence is OwnerPresence.ACTIVE:
        return BackgroundDecision(
            action=BackgroundAction.PAUSE,
            work_kind=work_kind,
            reason="owner_active",
        )
    if owner_presence is OwnerPresence.UNKNOWN:
        return BackgroundDecision(
            action=BackgroundAction.PAUSE,
            work_kind=work_kind,
            reason="owner_presence_unknown",
        )

    status = _validate_capacity(capacity)
    if status.under_pressure:
        return BackgroundDecision(
            action=BackgroundAction.DEFER,
            work_kind=work_kind,
            reason="resource_pressure",
        )
    if status.concurrency_headroom < 1:
        return BackgroundDecision(
            action=BackgroundAction.DEFER,
            work_kind=work_kind,
            reason="concurrency_exhausted",
        )
    if work_kind in _HIGH_IMPACT_WORK:
        if status.snapshot.power_plugged is None:
            return BackgroundDecision(
                action=BackgroundAction.DEFER,
                work_kind=work_kind,
                reason="power_state_unknown",
            )
        if not status.snapshot.power_plugged:
            return BackgroundDecision(
                action=BackgroundAction.DEFER,
                work_kind=work_kind,
                reason="battery_power",
            )

    return BackgroundDecision(
        action=BackgroundAction.RUN,
        work_kind=work_kind,
        reason="owner_away_capacity_available",
    )
