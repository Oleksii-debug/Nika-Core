from __future__ import annotations

from dataclasses import dataclass

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_build_execution import (
    BuildExecutionRecord,
    BuildExecutionState,
)
from nika_core.product_factory_coordinator import WorkState
from nika_core.product_factory_deployment import ExecutionNode
from nika_core.product_factory_packaged_build_authority import (
    PackagedBuildAuthorityRuntime,
)
from nika_core.product_factory_packaged_build_host import (
    build_packaged_local_durable_build_host,
)
from nika_core.product_factory_packaged_local_startup import (
    PackagedLocalProductFactoryStartup,
)
from nika_core.product_factory_packaged_preparation import PreparedProductFactory


class PackagedBuildPassError(RuntimeError):
    """Raised when the packaged PF4 -> PF5 pass cannot advance safely."""


@dataclass(frozen=True, slots=True)
class PackagedComponentBuildState:
    component_id: str
    work_id: str
    state: BuildExecutionState
    block_reason: str | None

    def __post_init__(self) -> None:
        if type(self.component_id) is not str or not self.component_id.strip():
            raise PackagedBuildPassError("component build state requires component identity")
        if type(self.work_id) is not str or not self.work_id.strip():
            raise PackagedBuildPassError("component build state requires work identity")
        if type(self.state) is not BuildExecutionState:
            raise PackagedBuildPassError("component build state requires exact PF5 state")
        if self.block_reason is not None and (
            type(self.block_reason) is not str or not self.block_reason.strip()
        ):
            raise PackagedBuildPassError("component build blocker must be non-empty text")


@dataclass(frozen=True, slots=True)
class PackagedBuildPassResult:
    project_id: str
    components: tuple[PackagedComponentBuildState, ...]

    def __post_init__(self) -> None:
        if type(self.project_id) is not str or not self.project_id.strip():
            raise PackagedBuildPassError("build pass requires project identity")
        component_ids = tuple(item.component_id for item in self.components)
        work_ids = tuple(item.work_id for item in self.components)
        if len(component_ids) != len(set(component_ids)):
            raise PackagedBuildPassError("build pass contains duplicate components")
        if len(work_ids) != len(set(work_ids)):
            raise PackagedBuildPassError("build pass contains duplicate PF5 work identities")


@dataclass(slots=True)
class PackagedReviewedBuildPass:
    """Advance exact independently reviewed PF4 work through durable local PF5.

    This is a composition layer only. Review/candidate identity remains owned by the
    incumbent ProductFactoryCoordinator, execution/output authority remains owned by
    PackagedBuildAuthorityRuntime, repository paths remain owned by durable local
    repository bindings, and DurableBuildExecutionHost remains the PF5 state/effect
    authority.

    One invocation is deliberately bounded. WAITING states are reported instead of
    spinning or fabricating retries. A dispatch with an uncertain outcome is inspected
    once through the canonical PF5 reconciliation path and never replayed.
    """

    store: SQLiteStore
    node: ExecutionNode
    startup: PackagedLocalProductFactoryStartup
    authority: PackagedBuildAuthorityRuntime
    configured_components: frozenset[tuple[str, str, str]] | None = None

    def __post_init__(self) -> None:
        if type(self.store) is not SQLiteStore:
            raise TypeError("store must be exact SQLiteStore")
        if type(self.node) is not ExecutionNode:
            raise TypeError("node must be exact ExecutionNode")
        if type(self.startup) is not PackagedLocalProductFactoryStartup:
            raise TypeError("startup must be exact PackagedLocalProductFactoryStartup")
        if type(self.authority) is not PackagedBuildAuthorityRuntime:
            raise TypeError("authority must be exact PackagedBuildAuthorityRuntime")
        if self.configured_components is not None:
            if type(self.configured_components) is not frozenset:
                raise TypeError("configured_components must be exact frozenset")
            for key in self.configured_components:
                if (
                    type(key) is not tuple
                    or len(key) != 3
                    or any(type(value) is not str or not value.strip() for value in key)
                ):
                    raise TypeError(
                        "configured component keys must be exact project/repository/component tuples"
                    )

    def advance(self, prepared: PreparedProductFactory) -> PackagedBuildPassResult:
        if type(prepared) is not PreparedProductFactory:
            raise TypeError("prepared must be exact PreparedProductFactory")
        state = prepared.state
        project_id = state.authority.project_id
        if prepared.project_id != project_id:
            raise PackagedBuildPassError("prepared Product Factory identity is inconsistent")

        accepted = tuple(
            sorted(
                (
                    record.request.component_id,
                    record.request.work_id,
                )
                for record in state.coordinator.snapshot().records
                if record.state is WorkState.ACCEPTED
            )
        )
        if len({component_id for component_id, _ in accepted}) != len(accepted):
            raise PackagedBuildPassError(
                "durable Product Factory has duplicate accepted component identities"
            )
        if not accepted:
            return PackagedBuildPassResult(project_id, ())

        if self.configured_components is not None:
            repositories = {
                component.component_id: component.repository_id
                for component in state.authority.graph.components
            }
            for component_id, _candidate_work_id in accepted:
                repository_id = repositories.get(component_id)
                if repository_id is None:
                    raise PackagedBuildPassError(
                        "accepted component is absent from current repository graph"
                    )
                if (
                    project_id,
                    repository_id,
                    component_id,
                ) not in self.configured_components:
                    raise PackagedBuildPassError(
                        "accepted component has no active packaged build configuration"
                    )

        host = build_packaged_local_durable_build_host(
            self.store,
            host_task_id=prepared.host_task_id,
            project_id=project_id,
            node=self.node,
            startup=self.startup,
            trusted_authority=self.authority.trusted_execution,
            output_policies=self.authority.output_policies,
        )
        results: list[PackagedComponentBuildState] = []
        pf5_work_ids: set[str] = set()
        for component_id, _candidate_work_id in accepted:
            spec = self.authority.admit_reviewed_component(
                authority=state.authority,
                coordinator=state.coordinator,
                component_id=component_id,
            )
            if spec.request.project_id != project_id:
                raise PackagedBuildPassError(
                    "reviewed PF5 admission returned another ProductProject"
                )
            if spec.request.work_id in pf5_work_ids:
                raise PackagedBuildPassError(
                    "reviewed PF5 admission returned a duplicate work identity"
                )
            pf5_work_ids.add(spec.request.work_id)

            record = host.submit(spec)
            record = _advance_one(host, record)
            results.append(
                PackagedComponentBuildState(
                    component_id=component_id,
                    work_id=record.spec.request.work_id,
                    state=record.state,
                    block_reason=record.block_reason,
                )
            )
        return PackagedBuildPassResult(project_id, tuple(results))


def _advance_one(host, record: BuildExecutionRecord) -> BuildExecutionRecord:
    """Take only bounded, idempotent PF5 transitions for one durable work item."""

    work_id = record.spec.request.work_id
    # submit -> prepare -> dispatch -> execute -> optional reconcile is the longest
    # safe path. The bound prevents accidental future state-machine changes from
    # turning one user command into an unbounded retry loop.
    for _ in range(6):
        state = record.state
        if state in {BuildExecutionState.SUCCEEDED, BuildExecutionState.FAILED}:
            return record
        if state in {
            BuildExecutionState.PENDING,
            BuildExecutionState.WAITING_FOR_NODE,
            BuildExecutionState.WAITING_FOR_AUTHORITY,
        }:
            record = host.prepare(work_id)
            if record.state in {
                BuildExecutionState.WAITING_FOR_NODE,
                BuildExecutionState.WAITING_FOR_AUTHORITY,
            }:
                return record
            continue
        if state is BuildExecutionState.PREPARED:
            host.begin_dispatch(work_id)
            record = host.execute(work_id)
            continue
        if state is BuildExecutionState.DISPATCHING:
            record = host.execute(work_id)
            continue
        if state in {
            BuildExecutionState.EFFECT_IN_FLIGHT,
            BuildExecutionState.RECONCILE_REQUIRED,
        }:
            record = host.reconcile(work_id)
            return record
        raise PackagedBuildPassError(f"unsupported durable PF5 state: {state!s}")
    raise PackagedBuildPassError(
        "durable PF5 work exceeded the bounded transition budget"
    )
