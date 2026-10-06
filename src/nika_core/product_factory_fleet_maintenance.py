from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Protocol

from nika_core.product_factory_deployment import ExecutionNodeRegistry
from nika_core.product_factory_deployment_fleet import (
    DeploymentFleetRecord,
    FleetState,
)
from nika_core.product_factory_operations import ProductOperationsCoordinator
from nika_core.product_factory_operations_contracts import ServiceHealth


class FleetMaintenanceError(ValueError):
    """Raised when PF3 rolling fleet-maintenance invariants are violated."""


class NodeMaintenanceAction(StrEnum):
    DRAIN = "drain"
    RESTART = "restart"
    VERIFY = "verify"
    RESUME = "resume"


class NodeMaintenanceState(StrEnum):
    PENDING = "pending"
    CORDONED = "cordoned"
    DRAINED = "drained"
    RESTARTED = "restarted"
    VERIFIED = "verified"
    SUCCEEDED = "succeeded"
    BLOCKED_ACTIVE_LEASE = "blocked_active_lease"
    BLOCKED_QUORUM = "blocked_quorum"
    BLOCKED_CREDENTIAL = "blocked_credential"
    RECONCILE_REQUIRED = "reconcile_required"
    FAILED = "failed"


class RollingMaintenanceState(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    RECONCILE_REQUIRED = "reconcile_required"
    FAILED = "failed"
    SUCCEEDED = "succeeded"


@dataclass(frozen=True, slots=True)
class ServiceMaintenanceBinding:
    service_id: str
    environment_id: str
    release_sha: str
    artifact_digest: str
    replica_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not all(value.strip() for value in (self.service_id, self.environment_id)):
            raise FleetMaintenanceError("maintenance service identity must not be empty")
        _validate_sha(self.release_sha)
        _validate_digest(self.artifact_digest)
        if not self.replica_ids or len(self.replica_ids) != len(set(self.replica_ids)):
            raise FleetMaintenanceError("maintenance replica bindings must be unique and non-empty")
        if any(not replica_id.strip() for replica_id in self.replica_ids):
            raise FleetMaintenanceError("maintenance replica identity must not be empty")


@dataclass(frozen=True, slots=True)
class NodeMaintenanceRequest:
    request_id: str
    plan_id: str
    project_id: str
    fleet_plan_id: str
    node_id: str
    action: NodeMaintenanceAction
    bindings: tuple[ServiceMaintenanceBinding, ...]
    reason: str
    approval_ref: str
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        identities = (
            self.request_id,
            self.plan_id,
            self.project_id,
            self.fleet_plan_id,
            self.node_id,
            self.reason,
            self.approval_ref,
        )
        if not all(value.strip() for value in identities):
            raise FleetMaintenanceError("node-maintenance identity/approval must not be empty")
        if not self.bindings or not self.evidence_refs:
            raise FleetMaintenanceError("node-maintenance bindings/evidence must not be empty")
        service_ids = [binding.service_id for binding in self.bindings]
        if len(service_ids) != len(set(service_ids)):
            raise FleetMaintenanceError("node-maintenance bindings contain duplicate services")


@dataclass(frozen=True, slots=True)
class NodeMaintenanceResult:
    applied: bool
    uncertain: bool
    evidence_refs: tuple[str, ...]
    verified_replica_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.evidence_refs or (self.applied and self.uncertain):
            raise FleetMaintenanceError("node-maintenance result is invalid")
        if len(self.verified_replica_ids) != len(set(self.verified_replica_ids)):
            raise FleetMaintenanceError("verified replica identities must be unique")
        if any(not replica_id.strip() for replica_id in self.verified_replica_ids):
            raise FleetMaintenanceError("verified replica identity must not be empty")


class NodeMaintenancePort(Protocol):
    def apply(self, request: NodeMaintenanceRequest) -> NodeMaintenanceResult: ...
    def inspect(self, request: NodeMaintenanceRequest) -> NodeMaintenanceResult: ...


class FleetViewPort(Protocol):
    def get(self, plan_id: str) -> DeploymentFleetRecord: ...


@dataclass(frozen=True, slots=True)
class RollingMaintenancePlan:
    plan_id: str
    project_id: str
    fleet_plan_id: str
    node_ids: tuple[str, ...]
    approval_ref: str
    reason: str
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        values = (
            self.plan_id,
            self.project_id,
            self.fleet_plan_id,
            self.approval_ref,
            self.reason,
        )
        if not all(value.strip() for value in values):
            raise FleetMaintenanceError("rolling-maintenance identity/approval must not be empty")
        if not self.node_ids or len(self.node_ids) != len(set(self.node_ids)):
            raise FleetMaintenanceError("rolling-maintenance nodes must be unique and non-empty")
        if any(not node_id.strip() for node_id in self.node_ids) or not self.evidence_refs:
            raise FleetMaintenanceError("rolling-maintenance nodes/evidence are invalid")


@dataclass(frozen=True, slots=True)
class NodeMaintenanceRecord:
    node_id: str
    state: NodeMaintenanceState
    bindings: tuple[ServiceMaintenanceBinding, ...]
    completed_actions: tuple[NodeMaintenanceAction, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    pending_request: NodeMaintenanceRequest | None = None
    cordoned: bool = False

    def __post_init__(self) -> None:
        if not self.node_id.strip() or not self.bindings:
            raise FleetMaintenanceError("node-maintenance record identity/bindings are invalid")
        expected_prefix = _ACTION_ORDER[: len(self.completed_actions)]
        if self.completed_actions != expected_prefix:
            raise FleetMaintenanceError("completed maintenance actions are not a valid prefix")
        if self.pending_request is not None:
            if self.state is not NodeMaintenanceState.RECONCILE_REQUIRED:
                raise FleetMaintenanceError("pending request requires reconcile state")
            if self.pending_request.node_id != self.node_id:
                raise FleetMaintenanceError("pending request belongs to another node")


@dataclass(frozen=True, slots=True)
class RollingMaintenanceRecord:
    plan: RollingMaintenancePlan
    state: RollingMaintenanceState
    nodes: tuple[NodeMaintenanceRecord, ...]


@dataclass(frozen=True, slots=True)
class RollingMaintenanceSnapshot:
    plans: tuple[RollingMaintenancePlan, ...]
    node_records: tuple[tuple[str, tuple[NodeMaintenanceRecord, ...]], ...]


_ACTION_ORDER = (
    NodeMaintenanceAction.DRAIN,
    NodeMaintenanceAction.RESTART,
    NodeMaintenanceAction.VERIFY,
    NodeMaintenanceAction.RESUME,
)


@dataclass(slots=True)
class RollingFleetMaintenanceCoordinator:
    fleet: FleetViewPort
    operations: ProductOperationsCoordinator
    nodes: ExecutionNodeRegistry
    port: NodeMaintenancePort
    _plans: dict[str, RollingMaintenancePlan] = field(default_factory=dict, init=False, repr=False)
    _records: dict[str, dict[str, NodeMaintenanceRecord]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    def submit(self, plan: RollingMaintenancePlan) -> RollingMaintenanceRecord:
        existing = self._plans.get(plan.plan_id)
        if existing is not None:
            if existing != plan:
                raise FleetMaintenanceError(
                    "rolling-maintenance plan id conflicts with prior payload"
                )
            return self.get(plan.plan_id)
        self._validate_project(plan)
        self._validate_start_nodes(plan)
        records: dict[str, NodeMaintenanceRecord] = {}
        for node_id in plan.node_ids:
            bindings = self._bindings(plan, node_id)
            if not bindings:
                raise FleetMaintenanceError("maintenance node has no project replicas")
            records[node_id] = NodeMaintenanceRecord(
                node_id,
                NodeMaintenanceState.PENDING,
                bindings,
            )
        self._plans[plan.plan_id] = plan
        self._records[plan.plan_id] = records
        return self.get(plan.plan_id)

    def advance(self, plan_id: str) -> RollingMaintenanceRecord:
        plan = self._plan(plan_id)
        records = self._records[plan_id]
        for node_id in plan.node_ids:
            record = records[node_id]
            if record.state is NodeMaintenanceState.SUCCEEDED:
                continue
            if record.state is NodeMaintenanceState.FAILED:
                return self.get(plan_id)
            live_bindings = self._bindings(plan, node_id)
            if live_bindings != record.bindings:
                raise FleetMaintenanceError(
                    "maintenance topology/release binding drifted from submitted plan"
                )
            if record.state is NodeMaintenanceState.RECONCILE_REQUIRED:
                records[node_id] = self._reconcile(record)
                return self.get(plan_id)
            action = _next_action(record)
            if action is None:
                return self.get(plan_id)
            blocked = self._blocked_state(record, action)
            if blocked is not None:
                records[node_id] = replace(record, state=blocked)
                return self.get(plan_id)
            if not record.completed_actions and not record.cordoned:
                self._set_node_enabled(node_id, enabled=False)
                record = replace(
                    record,
                    state=NodeMaintenanceState.CORDONED,
                    evidence_refs=record.evidence_refs
                    + (f"execution-node:cordoned:{node_id}",),
                    cordoned=True,
                )
                records[node_id] = record
            if not record.completed_actions:
                active_nodes = {lease.node_id for lease in self.nodes.snapshot().leases}
                if node_id in active_nodes:
                    records[node_id] = replace(
                        record,
                        state=NodeMaintenanceState.BLOCKED_ACTIVE_LEASE,
                    )
                    return self.get(plan_id)
            request = self._request(plan, record, action)
            result = self.port.apply(request)
            records[node_id] = self._consume_result(record, request, result)
            return self.get(plan_id)
        return self.get(plan_id)

    def get(self, plan_id: str) -> RollingMaintenanceRecord:
        plan = self._plan(plan_id)
        node_records = tuple(self._records[plan_id][node_id] for node_id in plan.node_ids)
        return RollingMaintenanceRecord(plan, _rolling_state(node_records), node_records)

    def snapshot(self) -> RollingMaintenanceSnapshot:
        return RollingMaintenanceSnapshot(
            tuple(self._plans[key] for key in sorted(self._plans)),
            tuple(
                (
                    plan_id,
                    tuple(
                        self._records[plan_id][node_id]
                        for node_id in self._plans[plan_id].node_ids
                    ),
                )
                for plan_id in sorted(self._plans)
            ),
        )

    def restore(self, snapshot: RollingMaintenanceSnapshot) -> None:
        plan_ids = [plan.plan_id for plan in snapshot.plans]
        record_plan_ids = [plan_id for plan_id, _ in snapshot.node_records]
        if (
            len(plan_ids) != len(set(plan_ids))
            or len(record_plan_ids) != len(set(record_plan_ids))
            or set(plan_ids) != set(record_plan_ids)
        ):
            raise FleetMaintenanceError("rolling-maintenance snapshot plan identities are invalid")
        plans = {plan.plan_id: plan for plan in snapshot.plans}
        records: dict[str, dict[str, NodeMaintenanceRecord]] = {}
        for plan_id, node_records in snapshot.node_records:
            plan = plans[plan_id]
            if tuple(record.node_id for record in node_records) != plan.node_ids:
                raise FleetMaintenanceError("rolling-maintenance snapshot node order is invalid")
            self._validate_project(plan)
            mapped: dict[str, NodeMaintenanceRecord] = {}
            for record in node_records:
                if self._bindings(plan, record.node_id) != record.bindings:
                    raise FleetMaintenanceError(
                        "rolling-maintenance snapshot disagrees with live topology"
                    )
                mapped[record.node_id] = record
            records[plan_id] = mapped
        self._plans = plans
        self._records = records
        self._validate_cordon_state()

    def _validate_project(self, plan: RollingMaintenancePlan) -> None:
        fleet = self.fleet.get(plan.fleet_plan_id)
        if fleet.plan.project_id != plan.project_id:
            raise FleetMaintenanceError("fleet maintenance belongs to another project")
        operations = self.operations.snapshot()
        if operations.project_id != plan.project_id:
            raise FleetMaintenanceError("operations state belongs to another project")
        known_nodes = {node.identity.node_id for node in self.nodes.snapshot().nodes}
        if not set(plan.node_ids) <= known_nodes:
            raise FleetMaintenanceError("maintenance plan references an unknown execution node")

    def _bindings(
        self,
        plan: RollingMaintenancePlan,
        node_id: str,
    ) -> tuple[ServiceMaintenanceBinding, ...]:
        fleet = self.fleet.get(plan.fleet_plan_id)
        by_fleet = {service.service_id: service for service in fleet.services}
        operations = self.operations.snapshot()
        bindings: list[ServiceMaintenanceBinding] = []
        for service_record in operations.services:
            service = service_record.service
            replicas = tuple(
                sorted(
                    replica.replica_id
                    for replica in service.replicas
                    if replica.node_id == node_id
                )
            )
            if not replicas:
                continue
            fleet_service = by_fleet.get(service.service_id)
            if fleet_service is None:
                raise FleetMaintenanceError("operations service is missing from fleet plan")
            if (
                service.project_id != plan.project_id
                or service.environment_id != fleet_service.environment_id
                or service.release_sha != fleet_service.release_sha
            ):
                raise FleetMaintenanceError(
                    "fleet and operations service identity/release provenance disagree"
                )
            if fleet_service.state not in {FleetState.HEALTHY, FleetState.DEGRADED}:
                raise FleetMaintenanceError(
                    "maintenance requires a deployed healthy/degraded fleet service"
                )
            bindings.append(
                ServiceMaintenanceBinding(
                    service.service_id,
                    service.environment_id,
                    fleet_service.release_sha,
                    fleet_service.artifact_digest,
                    replicas,
                )
            )
        return tuple(sorted(bindings, key=lambda item: item.service_id))

    def _blocked_state(
        self,
        record: NodeMaintenanceRecord,
        action: NodeMaintenanceAction,
    ) -> NodeMaintenanceState | None:
        operations = self.operations.snapshot()
        services = {item.service.service_id: item for item in operations.services}
        for binding in record.bindings:
            service_record = services[binding.service_id]
            if service_record.blocked_credentials or service_record.health is ServiceHealth.BLOCKED:
                return NodeMaintenanceState.BLOCKED_CREDENTIAL
        if action is not NodeMaintenanceAction.DRAIN:
            return None
        for binding in record.bindings:
            service_record = services[binding.service_id]
            if service_record.health in {
                ServiceHealth.PENDING,
                ServiceHealth.FAILED,
                ServiceHealth.ROLLBACK_REQUIRED,
                ServiceHealth.ROLLED_BACK,
            }:
                return NodeMaintenanceState.BLOCKED_QUORUM
            observation = service_record.observation
            if observation is None:
                return NodeMaintenanceState.BLOCKED_QUORUM
            healthy = set(observation.healthy_replica_ids) - set(service_record.node_loss)
            remaining = healthy - set(binding.replica_ids)
            if len(remaining) < service_record.service.min_healthy_replicas:
                return NodeMaintenanceState.BLOCKED_QUORUM
        return None

    def _request(
        self,
        plan: RollingMaintenancePlan,
        record: NodeMaintenanceRecord,
        action: NodeMaintenanceAction,
    ) -> NodeMaintenanceRequest:
        return NodeMaintenanceRequest(
            request_id=f"{plan.plan_id}:{record.node_id}:{action.value}",
            plan_id=plan.plan_id,
            project_id=plan.project_id,
            fleet_plan_id=plan.fleet_plan_id,
            node_id=record.node_id,
            action=action,
            bindings=record.bindings,
            reason=plan.reason,
            approval_ref=plan.approval_ref,
            evidence_refs=plan.evidence_refs + record.evidence_refs,
        )

    def _consume_result(
        self,
        record: NodeMaintenanceRecord,
        request: NodeMaintenanceRequest,
        result: NodeMaintenanceResult,
    ) -> NodeMaintenanceRecord:
        evidence = record.evidence_refs + result.evidence_refs
        if result.uncertain:
            if request.action is NodeMaintenanceAction.DRAIN:
                self.operations.record_node_availability(record.node_id, available=False)
            return replace(
                record,
                state=NodeMaintenanceState.RECONCILE_REQUIRED,
                evidence_refs=evidence,
                pending_request=request,
            )
        if not result.applied:
            return replace(record, state=NodeMaintenanceState.FAILED, evidence_refs=evidence)
        return self._complete_action(record, request.action, result, evidence)

    def _reconcile(self, record: NodeMaintenanceRecord) -> NodeMaintenanceRecord:
        request = record.pending_request
        if request is None:
            raise FleetMaintenanceError("reconcile state is missing durable request identity")
        result = self.port.inspect(request)
        evidence = record.evidence_refs + result.evidence_refs
        if result.uncertain:
            return replace(record, evidence_refs=evidence)
        if not result.applied:
            if request.action is NodeMaintenanceAction.DRAIN:
                self.operations.record_node_availability(record.node_id, available=True)
            return replace(
                record,
                state=NodeMaintenanceState.FAILED,
                evidence_refs=evidence,
                pending_request=None,
            )
        return self._complete_action(record, request.action, result, evidence)

    def _complete_action(
        self,
        record: NodeMaintenanceRecord,
        action: NodeMaintenanceAction,
        result: NodeMaintenanceResult,
        evidence: tuple[str, ...],
    ) -> NodeMaintenanceRecord:
        expected = _next_action(record)
        if expected is not action:
            raise FleetMaintenanceError("maintenance result action is out of order")
        if action is NodeMaintenanceAction.VERIFY:
            required = {
                replica_id
                for binding in record.bindings
                for replica_id in binding.replica_ids
            }
            if required != set(result.verified_replica_ids):
                raise FleetMaintenanceError(
                    "verification must prove exactly the drained replicas healthy"
                )
        if action is NodeMaintenanceAction.DRAIN:
            self.operations.record_node_availability(record.node_id, available=False)
        elif action is NodeMaintenanceAction.RESUME:
            self.operations.record_node_availability(record.node_id, available=True)
            self._set_node_enabled(record.node_id, enabled=True)
        actions = record.completed_actions + (action,)
        state = {
            NodeMaintenanceAction.DRAIN: NodeMaintenanceState.DRAINED,
            NodeMaintenanceAction.RESTART: NodeMaintenanceState.RESTARTED,
            NodeMaintenanceAction.VERIFY: NodeMaintenanceState.VERIFIED,
            NodeMaintenanceAction.RESUME: NodeMaintenanceState.SUCCEEDED,
        }[action]
        return replace(
            record,
            state=state,
            completed_actions=actions,
            evidence_refs=evidence,
            pending_request=None,
            cordoned=False if action is NodeMaintenanceAction.RESUME else record.cordoned,
        )

    def _set_node_enabled(self, node_id: str, *, enabled: bool) -> None:
        snapshot = self.nodes.snapshot()
        try:
            node = next(item for item in snapshot.nodes if item.identity.node_id == node_id)
        except StopIteration as exc:
            raise FleetMaintenanceError("unknown execution node") from exc
        self.nodes.register(replace(node, enabled=enabled))

    def _validate_start_nodes(self, plan: RollingMaintenancePlan) -> None:
        snapshot = self.nodes.snapshot()
        by_id = {node.identity.node_id: node for node in snapshot.nodes}
        if any(not by_id[node_id].enabled for node_id in plan.node_ids):
            raise FleetMaintenanceError(
                "rolling maintenance cannot take ownership of a disabled execution node"
            )
        for records in self._records.values():
            for record in records.values():
                if (
                    record.node_id in plan.node_ids
                    and record.state
                    not in {NodeMaintenanceState.SUCCEEDED, NodeMaintenanceState.FAILED}
                ):
                    raise FleetMaintenanceError(
                        "execution node already belongs to active rolling maintenance"
                    )

    def _validate_cordon_state(self) -> None:
        node_enabled = {
            node.identity.node_id: node.enabled for node in self.nodes.snapshot().nodes
        }
        for records in self._records.values():
            for record in records.values():
                enabled = node_enabled.get(record.node_id)
                if enabled is None:
                    raise FleetMaintenanceError(
                        "restored maintenance references an unknown execution node"
                    )
                if record.cordoned and enabled:
                    raise FleetMaintenanceError(
                        "restored maintenance node lost its cordon state"
                    )
                if record.state is NodeMaintenanceState.SUCCEEDED and not enabled:
                    raise FleetMaintenanceError(
                        "completed maintenance node must be enabled"
                    )

    def _plan(self, plan_id: str) -> RollingMaintenancePlan:
        plan = self._plans.get(plan_id)
        if plan is None:
            raise FleetMaintenanceError("unknown rolling-maintenance plan")
        return plan


def _next_action(record: NodeMaintenanceRecord) -> NodeMaintenanceAction | None:
    if len(record.completed_actions) == len(_ACTION_ORDER):
        return None
    return _ACTION_ORDER[len(record.completed_actions)]


def _rolling_state(records: tuple[NodeMaintenanceRecord, ...]) -> RollingMaintenanceState:
    states = {record.state for record in records}
    if states == {NodeMaintenanceState.SUCCEEDED}:
        return RollingMaintenanceState.SUCCEEDED
    if NodeMaintenanceState.FAILED in states:
        return RollingMaintenanceState.FAILED
    if NodeMaintenanceState.RECONCILE_REQUIRED in states:
        return RollingMaintenanceState.RECONCILE_REQUIRED
    if states & {
        NodeMaintenanceState.BLOCKED_ACTIVE_LEASE,
        NodeMaintenanceState.BLOCKED_QUORUM,
        NodeMaintenanceState.BLOCKED_CREDENTIAL,
    }:
        return RollingMaintenanceState.BLOCKED
    if states == {NodeMaintenanceState.PENDING}:
        return RollingMaintenanceState.PENDING
    return RollingMaintenanceState.IN_PROGRESS


def _validate_sha(value: str) -> None:
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise FleetMaintenanceError("release SHA must be a lowercase 40-character hex digest")


def _validate_digest(value: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise FleetMaintenanceError("artifact digest must be a lowercase 64-character hex digest")
