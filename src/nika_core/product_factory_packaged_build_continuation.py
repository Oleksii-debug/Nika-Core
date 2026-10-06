from __future__ import annotations

import asyncio
from dataclasses import dataclass

from nika_core.data.sqlite import SQLiteStore
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
    """Raised when PF4 -> packaged PF5 continuation cannot stay authoritative."""


@dataclass(frozen=True, slots=True)
class PackagedReviewedBuildContinuation:
    """Continue exact accepted PF4 components through the incumbent durable PF5 host.

    The prepared Product Factory state remains the PF4 authority. The launch-frozen
    packaged build runtime supplies only PF5 node/command/output policy. This adapter
    owns no build state, checkpoint, scheduler, repository authority or deployment
    authority.
    """

    store: SQLiteStore
    startup: PackagedLocalProductFactoryStartup
    activated: ActivatedPackagedBuildRuntime

    def __post_init__(self) -> None:
        if type(self.store) is not SQLiteStore:
            raise TypeError("PF5 continuation store must be exact SQLiteStore")
        if type(self.startup) is not PackagedLocalProductFactoryStartup:
            raise TypeError(
                "PF5 continuation requires exact packaged local startup authority"
            )
        if type(self.activated) is not ActivatedPackagedBuildRuntime:
            raise TypeError(
                "PF5 continuation requires exact ActivatedPackagedBuildRuntime"
            )
        self.startup.__post_init__()

    async def __call__(self, prepared: PreparedProductFactory) -> None:
        if type(prepared) is not PreparedProductFactory:
            raise TypeError("PF5 continuation requires exact PreparedProductFactory")
        await asyncio.to_thread(self._advance, prepared)

    def _advance(self, prepared: PreparedProductFactory) -> None:
        state = prepared.state
        project_id = state.authority.project_id
        snapshot = state.coordinator.snapshot()
        accepted = tuple(
            record
            for record in snapshot.records
            if record.state is WorkState.ACCEPTED
        )
        if not accepted:
            return

        # Validate the whole accepted batch before constructing or advancing PF5.
        # A partially configured launch must never build only a subset silently.
        for record in accepted:
            self.activated.require_component(
                project_id=project_id,
                repository_id=record.request.repository_id,
                component_id=record.request.component_id,
            )

        host = build_packaged_local_durable_build_host(
            self.store,
            host_task_id=prepared.host_task_id,
            project_id=project_id,
            node=self.activated.node,
            startup=self.startup,
            trusted_authority=self.activated.runtime.trusted_execution,
            output_policies=self.activated.runtime.output_policies,
        )
        controller = PackagedReviewedBuildLoopController(
            self.activated.runtime,
            host,
        )
        for record in accepted:
            advanced = controller.advance_component(
                state=state,
                component_id=record.request.component_id,
            )
            if advanced.spec.request.project_id != project_id:
                raise PackagedBuildContinuationError(
                    "PF5 continuation returned work for a different ProductProject"
                )
