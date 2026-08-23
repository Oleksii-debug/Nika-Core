from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from nika_core.product_factory_coding_worker_adapter import (
    CodingWorkerAdapterError,
    CodingWorkerComponentAdapter,
    component_task_id,
)
from nika_core.product_factory_coordinator import (
    ComponentWorkRequest,
    CoordinatorError,
    ProductFactoryCoordinator,
    WorkRecord,
    WorkState,
)
from nika_core.toolsmith.contracts import CandidateState, CapabilityGap, GapKind


class ProductFactoryToolsmithError(ValueError):
    """Raised when a component-scoped Toolsmith escalation cannot be reconciled safely."""


@dataclass(frozen=True, slots=True)
class ComponentCapabilityGap:
    """Durable identity binding one narrow capability gap to one component attempt."""

    work_id: str
    component_id: str
    task_id: str
    capability_id: str
    row_version: int
    state: CandidateState
    gap: CapabilityGap

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (self.work_id, self.component_id, self.task_id, self.capability_id)
        ):
            raise ProductFactoryToolsmithError(
                "component capability gap identity must not be empty"
            )
        if self.row_version < 0:
            raise ProductFactoryToolsmithError(
                "component capability gap row version must not be negative"
            )
        if self.gap.task_id != self.task_id:
            raise ProductFactoryToolsmithError(
                "capability gap task identity does not match component"
            )
        if self.gap.requested_capability != self.capability_id:
            raise ProductFactoryToolsmithError(
                "capability gap id does not match requested capability"
            )


@dataclass(frozen=True, slots=True)
class ComponentCapabilityResume:
    """Exact registered capability identity used to resume the same component task."""

    component_id: str
    previous_work_id: str
    next_request: ComponentWorkRequest
    capability_id: str
    capability_version: str
    capability_digest: str


class CapabilityEscalationPort(Protocol):
    def begin(self, gap: CapabilityGap) -> tuple[int, CandidateState]: ...

    def reconcile_resume(self, *, task_id: str, capability_id: str) -> dict[str, str] | None: ...


@dataclass(slots=True)
class ProductFactoryToolsmithBridge:
    """Component-scoped bridge from Product Factory into durable Toolsmith escalation.

    Toolsmith receives only the stable component task identity and the original permission
    ceiling. It never receives a whole ProductProject as one CodingJob. Registration is
    not enough to mutate Product Factory state: resumption is allowed only for the exact
    failed component attempt, after Toolsmith returns a pinned version+digest, and the
    repair base is derived from Product Factory's exact worker result evidence.
    """

    escalation: CapabilityEscalationPort
    worker_adapter: CodingWorkerComponentAdapter

    def begin_gap(
        self,
        request: ComponentWorkRequest,
        *,
        capability_id: str,
        reason: str,
        attempted_methods: tuple[str, ...] = (),
    ) -> ComponentCapabilityGap:
        if not capability_id.strip() or not reason.strip():
            raise ProductFactoryToolsmithError("capability id and gap reason must not be empty")
        task_id = component_task_id(request)
        gap = CapabilityGap(
            task_id=task_id,
            requested_capability=capability_id,
            kind=GapKind.MISSING_CAPABILITY,
            reason=reason,
            attempted_methods=attempted_methods,
            permission_ceiling=request.permission_ceiling,
        )
        version, state = self.escalation.begin(gap)
        return ComponentCapabilityGap(
            work_id=request.work_id,
            component_id=request.component_id,
            task_id=task_id,
            capability_id=capability_id,
            row_version=version,
            state=state,
            gap=gap,
        )

    def resume_registered_gap(
        self,
        coordinator: ProductFactoryCoordinator,
        checkpoint: ComponentCapabilityGap,
    ) -> ComponentCapabilityResume | None:
        record = _record_for_component(coordinator, checkpoint.component_id)
        if record.request.work_id != checkpoint.work_id:
            raise ProductFactoryToolsmithError(
                "capability-gap checkpoint belongs to a stale component attempt"
            )
        if component_task_id(record.request) != checkpoint.task_id:
            raise ProductFactoryToolsmithError("component task identity drifted before gap resume")
        if record.state is not WorkState.REPAIR_REQUIRED:
            raise ProductFactoryToolsmithError(
                "capability gap can resume only from repair_required component state"
            )

        registered = self.escalation.reconcile_resume(
            task_id=checkpoint.task_id,
            capability_id=checkpoint.capability_id,
        )
        if registered is None:
            return None
        _validate_registered_identity(checkpoint, registered)

        try:
            next_request = self.worker_adapter.prepare_safe_repair(
                coordinator,
                checkpoint.component_id,
                reason=(
                    "Toolsmith capability registered: "
                    f"{checkpoint.capability_id}@{registered['version']} "
                    f"digest={registered['digest']}"
                ),
            )
        except (CodingWorkerAdapterError, CoordinatorError) as exc:
            raise ProductFactoryToolsmithError(
                f"registered capability cannot safely resume component: {exc}"
            ) from exc

        return ComponentCapabilityResume(
            component_id=checkpoint.component_id,
            previous_work_id=checkpoint.work_id,
            next_request=next_request,
            capability_id=checkpoint.capability_id,
            capability_version=registered["version"],
            capability_digest=registered["digest"],
        )


def _record_for_component(
    coordinator: ProductFactoryCoordinator,
    component_id: str,
) -> WorkRecord:
    for record in coordinator.snapshot().records:
        if record.request.component_id == component_id:
            return record
    raise ProductFactoryToolsmithError(f"unknown component {component_id}")


def _validate_registered_identity(
    checkpoint: ComponentCapabilityGap,
    registered: dict[str, str],
) -> None:
    required = {"task_id", "capability_id", "version", "digest"}
    if set(registered) != required:
        raise ProductFactoryToolsmithError("Toolsmith resume identity has unexpected fields")
    if registered["task_id"] != checkpoint.task_id:
        raise ProductFactoryToolsmithError("Toolsmith resumed a different task identity")
    if registered["capability_id"] != checkpoint.capability_id:
        raise ProductFactoryToolsmithError("Toolsmith resumed a different capability identity")
    if not registered["version"].strip() or not registered["digest"].strip():
        raise ProductFactoryToolsmithError("Toolsmith resume lost pinned version or digest")
