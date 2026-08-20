from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from nika_core.product_factory_coordinator import (
    ComponentWorkRequest,
    CoordinatorError,
    ProductFactoryCoordinator,
    WorkerResultEnvelope,
    WorkRecord,
    WorkState,
)
from nika_core.toolsmith.contracts import RecoveryState


class WorkerRecoveryDisposition(StrEnum):
    RECOVERED = "recovered"
    BLOCKED_MISSING_STATE = "blocked_missing_state"


@dataclass(frozen=True, slots=True)
class WorkerRecoveryOutcome:
    component_id: str
    disposition: WorkerRecoveryDisposition
    record: WorkRecord
    recovery_state: RecoveryState | None


class ComponentRecoveryPort(Protocol):
    async def inspect(self, work_id: str) -> RecoveryState | None: ...

    async def recover(
        self,
        request: ComponentWorkRequest,
        state: RecoveryState,
    ) -> WorkerResultEnvelope: ...


@dataclass(slots=True)
class ProductFactoryWorkerRecovery:
    """Restart reconciliation for in-flight PF2 component work.

    Durable ProductProject persistence remains outside this service. The caller restores
    a coordinator snapshot, then this service reconciles only work that was already
    RUNNING by consulting the stable public coding-worker recovery boundary.
    """

    worker: ComponentRecoveryPort

    async def recover_running(
        self,
        coordinator: ProductFactoryCoordinator,
        component_id: str,
    ) -> WorkerRecoveryOutcome:
        record = _record_from_snapshot(coordinator, component_id)
        if record.state is not WorkState.RUNNING:
            raise CoordinatorError(
                f"component {component_id} must be running before worker recovery"
            )

        state = await self.worker.inspect(record.request.work_id)
        if state is None:
            blocked = coordinator.block(
                component_id,
                "worker recovery state is unavailable after restart; host reconciliation required",
            )
            return WorkerRecoveryOutcome(
                component_id=component_id,
                disposition=WorkerRecoveryDisposition.BLOCKED_MISSING_STATE,
                record=blocked,
                recovery_state=None,
            )

        envelope = await self.worker.recover(record.request, state)
        updated = coordinator.record_result(envelope)
        return WorkerRecoveryOutcome(
            component_id=component_id,
            disposition=WorkerRecoveryDisposition.RECOVERED,
            record=updated,
            recovery_state=state,
        )


def _record_from_snapshot(
    coordinator: ProductFactoryCoordinator,
    component_id: str,
) -> WorkRecord:
    for record in coordinator.snapshot().records:
        if record.request.component_id == component_id:
            return record
    raise CoordinatorError(f"unknown component {component_id}")
