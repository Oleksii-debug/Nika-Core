from __future__ import annotations

from nika_core.product_command.contracts import ProductStatusEntry, ProductStatusKind
from nika_core.product_factory_build_execution import (
    BuildExecutionSnapshot,
    BuildExecutionState,
)

_BLOCKER_STATES = frozenset(
    {
        BuildExecutionState.WAITING_FOR_AUTHORITY,
        BuildExecutionState.RECONCILE_REQUIRED,
        BuildExecutionState.FAILED,
    }
)

_STATE_DETAILS = {
    BuildExecutionState.PENDING: "PF5 build очікує підготовки.",
    BuildExecutionState.WAITING_FOR_NODE: (
        "PF5 build очікує доступний дозволений execution node."
    ),
    BuildExecutionState.WAITING_FOR_AUTHORITY: (
        "PF5 build очікує чинну trusted execution authority."
    ),
    BuildExecutionState.PREPARED: "PF5 build підготовлено до dispatch.",
    BuildExecutionState.DISPATCHING: "PF5 build переходить до зовнішнього effect boundary.",
    BuildExecutionState.EFFECT_IN_FLIGHT: (
        "PF5 build має durable effect-in-flight marker; blind replay заборонено."
    ),
    BuildExecutionState.RECONCILE_REQUIRED: (
        "Результат PF5 build невизначений; потрібна inspection-only reconciliation."
    ),
    BuildExecutionState.SUCCEEDED: "PF5 build завершено успішно.",
    BuildExecutionState.FAILED: "PF5 build завершився помилкою.",
}


def build_execution_status_entries(
    snapshot: BuildExecutionSnapshot,
) -> tuple[ProductStatusEntry, ...]:
    """Project validated PF5 records into bounded, secret-safe ProductProject status."""

    if type(snapshot) is not BuildExecutionSnapshot:
        raise TypeError("PF5 status requires exact BuildExecutionSnapshot")

    entries: list[ProductStatusEntry] = []
    for record in snapshot.records:
        state = record.state
        repository_id = record.spec.scope.repository_id
        label_prefix = "PF5 build: "
        available = 240 - len(label_prefix)
        display_repository = (
            repository_id
            if len(repository_id) <= available
            else repository_id[: available - 3] + "..."
        )
        detail = f"{_STATE_DETAILS[state]} Attempt: {record.attempt}."
        entries.append(
            ProductStatusEntry(
                kind=(
                    ProductStatusKind.BLOCKER
                    if state in _BLOCKER_STATES
                    else ProductStatusKind.BUILD
                ),
                item_id=record.spec.request.work_id,
                label=label_prefix + display_repository,
                state=state.value,
                detail=detail,
            )
        )
    return tuple(entries)
