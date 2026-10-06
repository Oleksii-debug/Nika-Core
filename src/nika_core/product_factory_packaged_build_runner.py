from __future__ import annotations

from dataclasses import dataclass, field

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskPayloadCorruptionError, TaskQueue
from nika_core.product_factory_build_execution import (
    BuildExecutionRecord,
    BuildExecutionSpec,
    BuildExecutionState,
)
from nika_core.product_factory_build_execution_host import DurableBuildExecutionHost
from nika_core.product_factory_deployment import ExecutionNode
from nika_core.product_factory_packaged_build_authority import (
    PackagedBuildAuthorityRuntime,
    PackagedBuildAuthorityStore,
)
from nika_core.product_factory_packaged_build_host import (
    build_packaged_local_durable_build_host,
)
from nika_core.product_factory_packaged_local_startup import (
    PackagedLocalProductFactoryStartup,
)
from nika_core.product_factory_packaged_preparation import PreparedProductFactory
from nika_core.product_factory_coordinator import WorkState


class PackagedReviewedBuildRunnerError(RuntimeError):
    """Raised when packaged PF4-reviewed work cannot safely enter durable PF5."""


@dataclass(frozen=True, slots=True)
class PackagedReviewedBuildOutcome:
    component_id: str
    candidate_work_id: str
    build_work_id: str
    state: BuildExecutionState
    artifact_digest: str | None


@dataclass(slots=True)
class PackagedReviewedBuildRunner:
    """Advance exact PF4 ACCEPTED work through the incumbent durable PF5 host.

    The runner creates no scheduler, execution policy, node authority or process
    adapter. A caller must supply the real host-owned ExecutionNode and packaged
    local startup authority. Build policy/argv/output scope come only from the
    durable PackagedBuildAuthorityStore introduced by the canonical PF5 authority
    lane.
    """

    store: SQLiteStore
    node: ExecutionNode
    startup: PackagedLocalProductFactoryStartup
    _authority: PackagedBuildAuthorityRuntime = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if type(self.store) is not SQLiteStore:
            raise TypeError("store must be exact SQLiteStore")
        if type(self.node) is not ExecutionNode:
            raise TypeError("node must be exact ExecutionNode")
        if type(self.startup) is not PackagedLocalProductFactoryStartup:
            raise TypeError("startup must be exact PackagedLocalProductFactoryStartup")
        self.startup.__post_init__()
        self._authority = PackagedBuildAuthorityRuntime(
            PackagedBuildAuthorityStore(
                self.store,
                node=self.node,
                startup=self.startup,
            )
        )

    def advance(
        self,
        prepared: PreparedProductFactory,
        *,
        max_count: int = 32,
    ) -> tuple[PackagedReviewedBuildOutcome, ...]:
        if type(prepared) is not PreparedProductFactory:
            raise TypeError("prepared must be exact PreparedProductFactory")
        if type(max_count) is not int or not 1 <= max_count <= 256:
            raise ValueError("packaged reviewed build max_count must be 1..256")
        self._require_host_task(prepared)

        accepted = tuple(
            sorted(
                (
                    record
                    for record in prepared.state.coordinator.snapshot().records
                    if record.state is WorkState.ACCEPTED
                ),
                key=lambda record: record.request.component_id,
            )
        )[:max_count]
        if not accepted:
            return ()

        host = build_packaged_local_durable_build_host(
            self.store,
            host_task_id=prepared.host_task_id,
            project_id=prepared.project_id,
            node=self.node,
            startup=self.startup,
            trusted_authority=self._authority.trusted_execution,
            output_policies=self._authority.output_policies,
        )
        outcomes: list[PackagedReviewedBuildOutcome] = []
        for candidate in accepted:
            spec = self._authority.admit_reviewed_component(
                authority=prepared.state.authority,
                coordinator=prepared.state.coordinator,
                component_id=candidate.request.component_id,
            )
            record = self._advance_one(host, spec.request.work_id, spec)
            outcomes.append(
                PackagedReviewedBuildOutcome(
                    component_id=candidate.request.component_id,
                    candidate_work_id=candidate.request.work_id,
                    build_work_id=spec.request.work_id,
                    state=record.state,
                    artifact_digest=(
                        None
                        if record.evidence is None
                        else record.evidence.artifact_digest
                    ),
                )
            )
        return tuple(outcomes)

    @staticmethod
    def _advance_one(
        host: DurableBuildExecutionHost,
        work_id: str,
        spec: BuildExecutionSpec,
    ) -> BuildExecutionRecord:
        record = host.submit(spec)
        if record.state in {
            BuildExecutionState.SUCCEEDED,
            BuildExecutionState.FAILED,
        }:
            return record
        if record.state in {
            BuildExecutionState.EFFECT_IN_FLIGHT,
            BuildExecutionState.RECONCILE_REQUIRED,
        }:
            return host.reconcile(work_id)

        record = host.prepare(work_id)
        if record.state in {
            BuildExecutionState.WAITING_FOR_NODE,
            BuildExecutionState.WAITING_FOR_AUTHORITY,
            BuildExecutionState.SUCCEEDED,
            BuildExecutionState.FAILED,
        }:
            return record
        if record.state in {
            BuildExecutionState.EFFECT_IN_FLIGHT,
            BuildExecutionState.RECONCILE_REQUIRED,
        }:
            return host.reconcile(work_id)
        if record.state is BuildExecutionState.PREPARED:
            host.begin_dispatch(work_id)
        record = host.execute(work_id)
        if record.state is BuildExecutionState.RECONCILE_REQUIRED:
            record = host.reconcile(work_id)
        return record

    def _require_host_task(self, prepared: PreparedProductFactory) -> None:
        try:
            task = TaskQueue(self.store).get(prepared.host_task_id)
        except (KeyError, TaskPayloadCorruptionError) as exc:
            raise PackagedReviewedBuildRunnerError(
                "prepared Product Factory host task is unavailable in runner storage"
            ) from exc
        if (
            task.payload.get("kind") != "product_factory"
            or task.payload.get("product_project_id") != prepared.project_id
        ):
            raise PackagedReviewedBuildRunnerError(
                "prepared Product Factory host task identity does not match runner storage"
            )
