from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from nika_core.resources.contracts import (
    ResourceBudget,
    ResourceCapacityStatus,
    ResourceSnapshot,
)

_BACKGROUND_SCOPE = "background_life"


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


def _validate_capacity(capacity: object) -> ResourceCapacityStatus:
    if type(capacity) is not ResourceCapacityStatus:
        raise TypeError("capacity must be exact ResourceCapacityStatus")
    if type(capacity.budget) is not ResourceBudget:
        raise TypeError("capacity.budget must be exact ResourceBudget")
    if type(capacity.snapshot) is not ResourceSnapshot:
        raise TypeError("capacity.snapshot must be exact ResourceSnapshot")
    if type(capacity.budget.scope) is not str:
        raise TypeError("capacity budget scope must be exact built-in str")
    if capacity.budget.scope != _BACKGROUND_SCOPE:
        raise ValueError(f"capacity budget scope must be {_BACKGROUND_SCOPE!r}")
    for name, value in (
        ("active_count", capacity.active_count),
        ("queued_count", capacity.queued_count),
        ("concurrency_headroom", capacity.concurrency_headroom),
    ):
        if type(value) is not int:
            raise TypeError(f"{name} must be exact built-in int")
        if value < 0:
            raise ValueError(f"{name} must not be negative")
    if type(capacity.pressure_reasons) is not tuple:
        raise TypeError("pressure_reasons must be exact tuple")
    for reason in capacity.pressure_reasons:
        if type(reason) is not str or not reason or reason != reason.strip():
            raise ValueError("pressure_reasons must contain canonical non-empty strings")
    if capacity.snapshot.power_plugged is not None:
        if type(capacity.snapshot.power_plugged) is not bool:
            raise TypeError("power_plugged must be exact built-in bool or None")
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
