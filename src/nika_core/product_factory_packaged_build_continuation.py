from __future__ import annotations

import asyncio
from dataclasses import dataclass

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_build_execution import (
    BuildExecutionRecord,
    BuildExecutionState,
)
from nika_core.product_factory_coordinator import WorkState
from nika_core.product_factory_packaged_build_host import (
    build_packaged_local_durable_build_host,
)
from nika_core.product_factory_packaged_build_loop import (
    PackagedReviewedBuildLoopController,
)
from nika_core.product_factory_packaged_build_settings import (
    ActivatedPackagedBuildRuntime,
)
from nika_core.product_factory_packaged_local_startup import (
    PackagedLocalProductFactoryStartup,
)
from nika_core.product_factory_packaged_preparation import PreparedProductFactory


class PackagedBuildContinuationError(RuntimeError):
    """Raised when packaged PF5 continuation cannot preserve exact authority."""


@dataclass(frozen=True, slots=True)
class PackagedReviewedBuildContinuation:
    """Advance accepted PF4 work through canonical durable PF5 on desktop.

    This class is composition glue only. It does not own review, build authority,
    checkpoints, process effects, repository identity or PF6 staging. Synchronous
    contained build effects run on a worker thread so the desktop event loop remains
    responsive.

    One invocation is conservative: components are visited in canonical dependency
    order and the pass stops at the first build that is not SUCCEEDED. Crash-left
    EFFECT_IN_FLIGHT/RECONCILE_REQUIRED work is inspected once through the canonical
    controller and is never blindly replayed.
    """

    store: SQLiteStore
    startup: PackagedLocalProductFactoryStartup
    activated: ActivatedPackagedBuildRuntime

    def __post_init__(self) -> None:
        if type(self.store) is not SQLiteStore:
            raise TypeError("PF5 continuation store must be exact SQLiteStore")
        if type(self.startup) is not PackagedLocalProductFactoryStartup:
            raise TypeError(
                "PF5 continuation startup must be exact "
                "PackagedLocalProductFactoryStartup"
            )
        if type(self.activated) is not ActivatedPackagedBuildRuntime:
            raise TypeError(
                "PF5 continuation activation must be exact "
                "ActivatedPackagedBuildRuntime"
            )

    async def __call__(self, prepared: PreparedProductFactory) -> None:
        if type(prepared) is not PreparedProductFactory:
            raise TypeError("PF5 continuation requires exact PreparedProductFactory")
        await asyncio.to_thread(self.advance, prepared)

    def advance(
        self,
        prepared: PreparedProductFactory,
    ) -> tuple[BuildExecutionRecord, ...]:
        """Advance accepted components without crossing the PF6 staging boundary."""

        if type(prepared) is not PreparedProductFactory:
            raise TypeError("PF5 continuation requires exact PreparedProductFactory")

        snapshot = prepared.state.coordinator.snapshot()
        accepted = tuple(
            record for record in snapshot.records if record.state is WorkState.ACCEPTED
        )
        if not accepted:
            return ()

        by_component = {record.request.component_id: record for record in accepted}
        if len(by_component) != len(accepted):
            raise PackagedBuildContinuationError(
                "accepted PF4 component identity is duplicated"
            )

        dependency_order = prepared.state.authority.graph.dependency_order()
        accepted_ids = frozenset(by_component)
        ordered_ids = tuple(
            component_id
            for component_id in dependency_order
            if component_id in accepted_ids
        )
        if len(ordered_ids) != len(accepted_ids):
            raise PackagedBuildContinuationError(
                "accepted PF4 component is outside the authoritative graph"
            )

        for component_id in ordered_ids:
            request = by_component[component_id].request
            if request.project_id != prepared.project_id:
                raise PackagedBuildContinuationError(
                    "accepted PF4 work belongs to another ProductProject"
                )
            self.activated.require_component(
                project_id=request.project_id,
                repository_id=request.repository_id,
                component_id=component_id,
            )

        build_host = build_packaged_local_durable_build_host(
            self.store,
            host_task_id=prepared.host_task_id,
            project_id=prepared.project_id,
            node=self.activated.node,
            startup=self.startup,
            trusted_authority=self.activated.runtime.trusted_execution,
            output_policies=self.activated.runtime.output_policies,
        )
        controller = PackagedReviewedBuildLoopController(
            authorities=self.activated.runtime,
            build_host=build_host,
        )

        advanced: list[BuildExecutionRecord] = []
        for component_id in ordered_ids:
            record = controller.advance_component(
                state=prepared.state,
                component_id=component_id,
            )
            if record.state in {
                BuildExecutionState.EFFECT_IN_FLIGHT,
                BuildExecutionState.RECONCILE_REQUIRED,
            }:
                record = controller.reconcile_work(record.spec.request.work_id)
            advanced.append(record)
            if record.state is not BuildExecutionState.SUCCEEDED:
                break
        return tuple(advanced)
