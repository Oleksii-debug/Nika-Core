from __future__ import annotations

from dataclasses import dataclass

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_build_execution import (
    BuildExecutionRecord,
    BuildExecutionState,
)
from nika_core.product_factory_coordinator import WorkRecord, WorkState
from nika_core.product_factory_packaged_build_settings import (
    ActivatedPackagedBuildRuntime,
    ConfiguredPackagedReviewedBuildController,
    PackagedBuildRuntimeSettingsError,
    build_configured_packaged_reviewed_build_controller,
)
from nika_core.product_factory_packaged_local_startup import (
    PackagedLocalProductFactoryStartup,
)
from nika_core.product_factory_packaged_preparation import PreparedProductFactory


class PackagedAcceptedBuildContinuationError(RuntimeError):
    """Raised when reviewed PF4 work cannot safely continue through packaged PF5."""


@dataclass(frozen=True, slots=True)
class PackagedAcceptedBuildResult:
    """One bounded explicit continuation of accepted PF4 work through local PF5."""

    project_id: str
    records: tuple[BuildExecutionRecord, ...]


@dataclass(slots=True)
class PackagedAcceptedBuildContinuation:
    """Advance only exact accepted PF4 work through the canonical packaged PF5 host.

    Product Factory PF4 remains the review/state authority. The launch-frozen PF5
    activation remains the build-command/output authority. This adapter only joins
    those incumbent authorities after the packaged execution controller has completed
    its PF4 recovery/dispatch pass.

    One call is bounded by max_components. All accepted components are preflighted
    against the launch-frozen PF5 membership before any PF5 effect starts, preventing
    a partially configured project from producing a partial build. An uncertain PF5
    effect is inspected at most once and is never replayed by this adapter.
    """

    store: SQLiteStore
    startup: PackagedLocalProductFactoryStartup
    activation: ActivatedPackagedBuildRuntime
    max_components: int = 32

    def __post_init__(self) -> None:
        if type(self.store) is not SQLiteStore:
            raise TypeError("store must be exact SQLiteStore")
        if type(self.startup) is not PackagedLocalProductFactoryStartup:
            raise TypeError("startup must be exact PackagedLocalProductFactoryStartup")
        self.startup.__post_init__()
        if type(self.activation) is not ActivatedPackagedBuildRuntime:
            raise TypeError("activation must be exact ActivatedPackagedBuildRuntime")
        self.activation.__post_init__()
        if (
            type(self.max_components) is not int
            or not 1 <= self.max_components <= 256
        ):
            raise ValueError("max_components must be an exact integer in 1..256")

    async def run_after_dispatch(self, prepared: PreparedProductFactory) -> None:
        """Async hook for PackagedProductFactoryExecutionController.post_dispatch."""

        self.advance(prepared)

    def advance(
        self,
        prepared: PreparedProductFactory,
    ) -> PackagedAcceptedBuildResult:
        if type(prepared) is not PreparedProductFactory:
            raise TypeError("prepared must be exact PreparedProductFactory")
        state = prepared.state
        project_id = state.authority.project_id
        if prepared.project_id != project_id:
            raise PackagedAcceptedBuildContinuationError(
                "prepared Product Factory identity is inconsistent"
            )

        accepted = _accepted_records(prepared)
        if len(accepted) > self.max_components:
            raise PackagedAcceptedBuildContinuationError(
                "accepted Product Factory build set exceeds the bounded continuation"
            )
        if not accepted:
            return PackagedAcceptedBuildResult(project_id, ())

        _preflight_membership(
            activation=self.activation,
            prepared=prepared,
            accepted=accepted,
        )
        configured = build_configured_packaged_reviewed_build_controller(
            self.store,
            host_task_id=prepared.host_task_id,
            project_id=project_id,
            startup=self.startup,
            activation=self.activation,
        )
        if type(configured) is not ConfiguredPackagedReviewedBuildController:
            raise PackagedAcceptedBuildContinuationError(
                "packaged PF5 controller composition returned a noncanonical carrier"
            )

        built: list[BuildExecutionRecord] = []
        for work in accepted:
            record = configured.advance_component(
                state=state,
                component_id=work.request.component_id,
            )
            if type(record) is not BuildExecutionRecord:
                raise PackagedAcceptedBuildContinuationError(
                    "packaged PF5 controller returned a noncanonical build record"
                )
            if record.state in {
                BuildExecutionState.EFFECT_IN_FLIGHT,
                BuildExecutionState.RECONCILE_REQUIRED,
            }:
                record = configured.controller.reconcile_work(
                    record.spec.request.work_id
                )
                if type(record) is not BuildExecutionRecord:
                    raise PackagedAcceptedBuildContinuationError(
                        "packaged PF5 reconciliation returned a noncanonical record"
                    )
            built.append(record)
        return PackagedAcceptedBuildResult(project_id, tuple(built))


def _accepted_records(
    prepared: PreparedProductFactory,
) -> tuple[WorkRecord, ...]:
    state = prepared.state
    snapshot = state.coordinator.snapshot()
    accepted = tuple(
        record
        for record in snapshot.records
        if record.state is WorkState.ACCEPTED
    )
    component_ids = tuple(record.request.component_id for record in accepted)
    work_ids = tuple(record.request.work_id for record in accepted)
    if len(component_ids) != len(set(component_ids)):
        raise PackagedAcceptedBuildContinuationError(
            "durable Product Factory has duplicate accepted component identities"
        )
    if len(work_ids) != len(set(work_ids)):
        raise PackagedAcceptedBuildContinuationError(
            "durable Product Factory has duplicate accepted work identities"
        )

    graph = state.authority.graph
    graph_order = graph.dependency_order()
    order = {component_id: index for index, component_id in enumerate(graph_order)}
    if any(component_id not in order for component_id in component_ids):
        raise PackagedAcceptedBuildContinuationError(
            "accepted Product Factory component is outside repository graph authority"
        )
    return tuple(
        sorted(
            accepted,
            key=lambda record: (
                order[record.request.component_id],
                record.request.component_id,
                record.request.work_id,
            ),
        )
    )


def _preflight_membership(
    *,
    activation: ActivatedPackagedBuildRuntime,
    prepared: PreparedProductFactory,
    accepted: tuple[WorkRecord, ...],
) -> None:
    state = prepared.state
    project_id = state.authority.project_id
    graph_components = {
        component.component_id: component
        for component in state.authority.graph.components
    }
    if len(graph_components) != len(state.authority.graph.components):
        raise PackagedAcceptedBuildContinuationError(
            "repository graph contains duplicate component identities"
        )

    for record in accepted:
        request = record.request
        if request.project_id != project_id:
            raise PackagedAcceptedBuildContinuationError(
                "accepted work belongs to another ProductProject"
            )
        component = graph_components.get(request.component_id)
        if component is None or component.repository_id != request.repository_id:
            raise PackagedAcceptedBuildContinuationError(
                "accepted work repository identity diverged from graph authority"
            )
        try:
            activation.require_component(
                project_id=project_id,
                repository_id=component.repository_id,
                component_id=component.component_id,
            )
        except PackagedBuildRuntimeSettingsError:
            raise
