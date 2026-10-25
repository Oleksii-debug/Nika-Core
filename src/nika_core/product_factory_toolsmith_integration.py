from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Protocol

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_checkpoint_host import (
    ProductFactoryCheckpointError,
    ProductFactoryCheckpointHost,
)
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
from nika_core.product_factory_project_binding import ProductProjectCoordinatorBinding
from nika_core.product_factory_toolsmith_state import (
    ComponentCapabilityBinding,
    ComponentCapabilityBindingState,
    ProductFactoryToolsmithBindingError,
    ProductFactoryToolsmithBindingRepository,
)
from nika_core.toolsmith.contracts import CandidateState, CapabilityGap, GapKind


class ProductFactoryToolsmithError(ValueError):
    """Raised when a component-scoped Toolsmith escalation cannot be reconciled safely."""


@dataclass(frozen=True, slots=True)
class ComponentCapabilityGap:
    """Identity binding one narrow capability gap to one component attempt."""

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
            for value in (
                self.work_id,
                self.component_id,
                self.task_id,
                self.capability_id,
            )
        ):
            raise ProductFactoryToolsmithError(
                "component capability gap identity must not be empty"
            )
        if self.row_version < 0:
            raise ProductFactoryToolsmithError(
                "component capability gap row version must be non-negative"
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

    def reconcile_resume(
        self,
        *,
        task_id: str,
        capability_id: str,
    ) -> dict[str, str] | None: ...


@dataclass(slots=True)
class ProductFactoryToolsmithBridge:
    """Component-scoped Product Factory ↔ Toolsmith integration.

    ``begin_gap`` / ``resume_registered_gap`` preserve the original in-memory
    compatibility surface for non-persistent callers. Production restart-safe
    composition supplies ``store`` and must use ``begin_durable_gap`` plus
    ``resume_durable_registered_gap`` with the canonical Product Factory host task.
    """

    escalation: CapabilityEscalationPort
    worker_adapter: CodingWorkerComponentAdapter
    store: SQLiteStore | None = None
    _bindings: ProductFactoryToolsmithBindingRepository | None = field(
        init=False,
        default=None,
        repr=False,
    )
    _checkpoints: ProductFactoryCheckpointHost | None = field(
        init=False,
        default=None,
        repr=False,
    )

    def __post_init__(self) -> None:
        if self.store is not None:
            self._bindings = ProductFactoryToolsmithBindingRepository(self.store)
            self._checkpoints = ProductFactoryCheckpointHost(self.store)

    def begin_gap(
        self,
        request: ComponentWorkRequest,
        *,
        capability_id: str,
        reason: str,
        attempted_methods: tuple[str, ...] = (),
    ) -> ComponentCapabilityGap:
        """Compatibility primitive whose checkpoint must remain with the caller."""
        if self.store is not None:
            raise ProductFactoryToolsmithError(
                "configured durable bridge requires begin_durable_gap(host_task_id=...)"
            )
        _validate_gap_input(capability_id, reason)
        task_id = component_task_id(request)
        gap = _gap_for_request(
            request,
            task_id=task_id,
            capability_id=capability_id,
            reason=reason,
            attempted_methods=attempted_methods,
        )
        version, state = self.escalation.begin(gap)
        return _component_gap(
            request,
            gap,
            version=version,
            state=state,
        )

    def begin_durable_gap(
        self,
        request: ComponentWorkRequest,
        *,
        host_task_id: str,
        capability_id: str,
        reason: str,
        attempted_methods: tuple[str, ...] = (),
    ) -> ComponentCapabilityGap:
        """Reserve an already-durable failed PF attempt before Toolsmith escalation."""
        _validate_gap_input(capability_id, reason)
        self._require_durable_failed_request(
            host_task_id=host_task_id,
            request=request,
        )
        bindings = self._require_bindings()
        try:
            durable = bindings.reserve(
                host_task_id=host_task_id,
                request=request,
                capability_id=capability_id,
                reason=reason,
                attempted_methods=attempted_methods,
            )
        except ProductFactoryToolsmithBindingError as exc:
            raise ProductFactoryToolsmithError(str(exc)) from exc

        if durable.state is ComponentCapabilityBindingState.CONSUMED:
            raise ProductFactoryToolsmithError(
                "capability gap for this component attempt is already consumed"
            )
        gap = _gap_from_binding(durable)
        if durable.state is ComponentCapabilityBindingState.RESERVED:
            version, state = self.escalation.begin(gap)
            try:
                durable = bindings.mark_begun(
                    durable,
                    escalation_row_version=version,
                    candidate_state=state.value,
                )
            except ProductFactoryToolsmithBindingError as exc:
                raise ProductFactoryToolsmithError(str(exc)) from exc
        return _component_gap_from_binding(durable)

    def resume_registered_gap(
        self,
        coordinator: ProductFactoryCoordinator,
        checkpoint: ComponentCapabilityGap,
    ) -> ComponentCapabilityResume | None:
        """Compatibility primitive for a caller-retained gap checkpoint."""
        if self.store is not None:
            raise ProductFactoryToolsmithError(
                "configured durable bridge requires resume_durable_registered_gap"
            )
        record = _record_for_component(coordinator, checkpoint.component_id)
        if record.request.work_id != checkpoint.work_id:
            raise ProductFactoryToolsmithError(
                "capability-gap checkpoint belongs to a stale component attempt"
            )
        if component_task_id(record.request) != checkpoint.task_id:
            raise ProductFactoryToolsmithError(
                "component task identity drifted before gap resume"
            )
        _require_repair_required(record)

        registered = self.escalation.reconcile_resume(
            task_id=checkpoint.task_id,
            capability_id=checkpoint.capability_id,
        )
        if registered is None:
            return None
        _validate_registered_identity(checkpoint, registered)
        return self._prepare_resume(coordinator, checkpoint, registered)

    def resume_durable_registered_gap(
        self,
        *,
        host_task_id: str,
        binding: ProductProjectCoordinatorBinding,
        coordinator: ProductFactoryCoordinator,
        component_id: str,
    ) -> ComponentCapabilityResume | None:
        """Restart-safe registered capability → exact Product Factory repair transition.

        The durable handoff uses two phases around the Product Factory checkpoint:

        1. persist exact previous/next work IDs plus pinned capability evidence;
        2. persist the coordinator repair checkpoint;
        3. mark the handoff consumed.

        Restart reconciles either crash window without creating another repair attempt.
        """
        bindings = self._require_bindings()
        checkpoints = self._require_checkpoints()
        record = _record_for_component(coordinator, component_id)
        try:
            durable = bindings.find_for_request(
                host_task_id=host_task_id,
                request=record.request,
            )
        except ProductFactoryToolsmithBindingError as exc:
            raise ProductFactoryToolsmithError(str(exc)) from exc
        if durable is None:
            return None

        durable = self._ensure_gap_begun(bindings, durable)
        checkpoint = _component_gap_from_binding(durable)

        registered = self.escalation.reconcile_resume(
            task_id=checkpoint.task_id,
            capability_id=checkpoint.capability_id,
        )
        if registered is None:
            if durable.state in {
                ComponentCapabilityBindingState.RESUME_PREPARED,
                ComponentCapabilityBindingState.CONSUMED,
            }:
                raise ProductFactoryToolsmithError(
                    "durable Product Factory resume references a capability "
                    "that is no longer registered"
                )
            return None
        _validate_registered_identity(checkpoint, registered)

        if durable.state is ComponentCapabilityBindingState.CONSUMED:
            _validate_prepared_pins(durable, registered)
            return _consumed_resume(record, durable)

        if durable.state is ComponentCapabilityBindingState.RESUME_PREPARED:
            _validate_prepared_pins(durable, registered)
            if record.request.work_id == durable.next_work_id:
                return self._finalize_prepared_resume(
                    bindings=bindings,
                    durable=durable,
                    record=record,
                )
            if record.request.work_id != durable.work_id:
                raise ProductFactoryToolsmithError(
                    "prepared capability resume no longer matches Product Factory attempt"
                )

        before = coordinator.snapshot()
        checkpoint_saved = False
        try:
            resume = self._prepare_resume(
                coordinator,
                checkpoint,
                registered,
            )
            if durable.state is ComponentCapabilityBindingState.RESUME_PREPARED:
                if resume.next_request.work_id != durable.next_work_id:
                    raise ProductFactoryToolsmithError(
                        "replayed repair request differs from durable prepared resume"
                    )
            else:
                durable = bindings.prepare_resume(
                    durable,
                    next_work_id=resume.next_request.work_id,
                    pinned_version=resume.capability_version,
                    pinned_digest=resume.capability_digest,
                )
            checkpoints.save(
                host_task_id=host_task_id,
                checkpoint=binding.checkpoint(coordinator),
            )
            checkpoint_saved = True
        finally:
            if not checkpoint_saved:
                coordinator.restore(before)

        try:
            bindings.mark_consumed(durable)
        except (ProductFactoryToolsmithBindingError, sqlite3.Error) as exc:
            raise ProductFactoryToolsmithError(
                "Product Factory repair checkpoint is durable but Toolsmith binding "
                "finalization requires restart reconciliation"
            ) from exc
        return resume

    def _require_durable_failed_request(
        self,
        *,
        host_task_id: str,
        request: ComponentWorkRequest,
    ) -> WorkRecord:
        checkpoints = self._require_checkpoints()
        try:
            persisted = checkpoints.latest(
                host_task_id=host_task_id,
                project_id=request.project_id,
            )
        except ProductFactoryCheckpointError as exc:
            raise ProductFactoryToolsmithError(
                f"durable Product Factory checkpoint is not authoritative: {exc}"
            ) from exc
        if persisted is None:
            raise ProductFactoryToolsmithError(
                "capability escalation requires a durable Product Factory checkpoint"
            )
        matches = tuple(
            record
            for record in persisted.checkpoint.coordinator.records
            if record.request.component_id == request.component_id
        )
        if len(matches) != 1:
            raise ProductFactoryToolsmithError(
                "durable Product Factory checkpoint has ambiguous component identity"
            )
        record = matches[0]
        if record.request != request:
            raise ProductFactoryToolsmithError(
                "capability escalation request does not match the durable failed attempt"
            )
        _require_repair_required(record)
        if record.result is None or record.result.coding_result.failure is None:
            raise ProductFactoryToolsmithError(
                "capability escalation requires durable worker-failure evidence"
            )
        return record

    def _ensure_gap_begun(
        self,
        bindings: ProductFactoryToolsmithBindingRepository,
        durable: ComponentCapabilityBinding,
    ) -> ComponentCapabilityBinding:
        if durable.state is not ComponentCapabilityBindingState.RESERVED:
            return durable
        gap = _gap_from_binding(durable)
        version, state = self.escalation.begin(gap)
        try:
            return bindings.mark_begun(
                durable,
                escalation_row_version=version,
                candidate_state=state.value,
            )
        except ProductFactoryToolsmithBindingError as exc:
            raise ProductFactoryToolsmithError(str(exc)) from exc

    def _finalize_prepared_resume(
        self,
        *,
        bindings: ProductFactoryToolsmithBindingRepository,
        durable: ComponentCapabilityBinding,
        record: WorkRecord,
    ) -> ComponentCapabilityResume:
        if record.state is not WorkState.READY:
            raise ProductFactoryToolsmithError(
                "durably resumed component is not in the expected ready state"
            )
        if not durable.pinned_version or not durable.pinned_digest:
            raise ProductFactoryToolsmithError(
                "prepared resume lost pinned capability evidence"
            )
        try:
            bindings.mark_consumed(durable)
        except (ProductFactoryToolsmithBindingError, sqlite3.Error) as exc:
            raise ProductFactoryToolsmithError(
                "prepared capability resume could not finalize durable binding"
            ) from exc
        return ComponentCapabilityResume(
            component_id=record.request.component_id,
            previous_work_id=durable.work_id,
            next_request=record.request,
            capability_id=durable.capability_id,
            capability_version=durable.pinned_version,
            capability_digest=durable.pinned_digest,
        )

    def _prepare_resume(
        self,
        coordinator: ProductFactoryCoordinator,
        checkpoint: ComponentCapabilityGap,
        registered: dict[str, str],
    ) -> ComponentCapabilityResume:
        try:
            next_request = self.worker_adapter.prepare_safe_repair(
                coordinator,
                checkpoint.component_id,
                reason=_repair_reason(
                    checkpoint.capability_id,
                    registered,
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

    def _require_bindings(self) -> ProductFactoryToolsmithBindingRepository:
        if self._bindings is None:
            raise ProductFactoryToolsmithError(
                "durable Toolsmith integration requires the canonical SQLiteStore"
            )
        return self._bindings

    def _require_checkpoints(self) -> ProductFactoryCheckpointHost:
        if self._checkpoints is None:
            raise ProductFactoryToolsmithError(
                "durable Toolsmith integration requires the Product Factory checkpoint host"
            )
        return self._checkpoints


def _record_for_component(
    coordinator: ProductFactoryCoordinator,
    component_id: str,
) -> WorkRecord:
    for record in coordinator.snapshot().records:
        if record.request.component_id == component_id:
            return record
    raise ProductFactoryToolsmithError(f"unknown component {component_id}")


def _require_repair_required(record: WorkRecord) -> None:
    if record.state is not WorkState.REPAIR_REQUIRED:
        raise ProductFactoryToolsmithError(
            "capability gap can resume only from repair_required component state"
        )


def _validate_registered_identity(
    checkpoint: ComponentCapabilityGap,
    registered: dict[str, str],
) -> None:
    required = {"task_id", "capability_id", "version", "digest"}
    if set(registered) != required:
        raise ProductFactoryToolsmithError(
            "Toolsmith resume identity has unexpected fields"
        )
    if registered["task_id"] != checkpoint.task_id:
        raise ProductFactoryToolsmithError(
            "Toolsmith resumed a different task identity"
        )
    if registered["capability_id"] != checkpoint.capability_id:
        raise ProductFactoryToolsmithError(
            "Toolsmith resumed a different capability identity"
        )
    if not registered["version"].strip() or not registered["digest"].strip():
        raise ProductFactoryToolsmithError(
            "Toolsmith resume lost pinned version or digest"
        )


def _validate_gap_input(capability_id: str, reason: str) -> None:
    if not capability_id.strip() or not reason.strip():
        raise ProductFactoryToolsmithError(
            "capability id and gap reason must not be empty"
        )


def _gap_for_request(
    request: ComponentWorkRequest,
    *,
    task_id: str,
    capability_id: str,
    reason: str,
    attempted_methods: tuple[str, ...],
) -> CapabilityGap:
    return CapabilityGap(
        task_id=task_id,
        requested_capability=capability_id,
        kind=GapKind.MISSING_CAPABILITY,
        reason=reason,
        attempted_methods=attempted_methods,
        permission_ceiling=request.permission_ceiling,
    )


def _gap_from_binding(binding: ComponentCapabilityBinding) -> CapabilityGap:
    return CapabilityGap(
        task_id=binding.host_task_id,
        requested_capability=binding.capability_id,
        kind=GapKind.MISSING_CAPABILITY,
        reason=binding.reason,
        attempted_methods=binding.attempted_methods,
        permission_ceiling=binding.permission_ceiling,
    )


def _component_gap(
    request: ComponentWorkRequest,
    gap: CapabilityGap,
    *,
    version: int,
    state: CandidateState,
) -> ComponentCapabilityGap:
    return ComponentCapabilityGap(
        work_id=request.work_id,
        component_id=request.component_id,
        task_id=gap.task_id,
        capability_id=gap.requested_capability,
        row_version=version,
        state=state,
        gap=gap,
    )


def _component_gap_from_binding(
    binding: ComponentCapabilityBinding,
) -> ComponentCapabilityGap:
    if binding.escalation_row_version is None or binding.candidate_state is None:
        raise ProductFactoryToolsmithError(
            "durable capability binding has not begun Toolsmith escalation"
        )
    try:
        state = CandidateState(binding.candidate_state)
    except ValueError as exc:
        raise ProductFactoryToolsmithError(
            "durable capability binding contains unknown Toolsmith state"
        ) from exc
    return ComponentCapabilityGap(
        work_id=binding.work_id,
        component_id=binding.component_id,
        task_id=binding.host_task_id,
        capability_id=binding.capability_id,
        row_version=binding.escalation_row_version,
        state=state,
        gap=_gap_from_binding(binding),
    )


def _validate_prepared_pins(
    durable: ComponentCapabilityBinding,
    registered: dict[str, str],
) -> None:
    if (
        durable.pinned_version != registered["version"]
        or durable.pinned_digest != registered["digest"]
    ):
        raise ProductFactoryToolsmithError(
            "registered capability identity changed after resume preparation"
        )


def _consumed_resume(
    record: WorkRecord,
    durable: ComponentCapabilityBinding,
) -> ComponentCapabilityResume:
    if (
        record.request.work_id != durable.next_work_id
        or record.state is not WorkState.READY
    ):
        raise ProductFactoryToolsmithError(
            "consumed capability binding no longer matches resumed Product Factory request"
        )
    if not durable.pinned_version or not durable.pinned_digest:
        raise ProductFactoryToolsmithError(
            "consumed binding lost pinned capability evidence"
        )
    return ComponentCapabilityResume(
        component_id=record.request.component_id,
        previous_work_id=durable.work_id,
        next_request=record.request,
        capability_id=durable.capability_id,
        capability_version=durable.pinned_version,
        capability_digest=durable.pinned_digest,
    )


def _repair_reason(
    capability_id: str,
    registered: dict[str, str],
) -> str:
    return (
        "Toolsmith capability registered: "
        f"{capability_id}@{registered['version']} digest={registered['digest']}"
    )
