from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from nika_core.product_factory_deployment import EnvironmentTier, ReleaseRef
from nika_core.product_factory_deployment_execution import (
    DeploymentExecutionRecord,
    DeploymentExecutionSpec,
    OperationState,
)


class DeploymentFleetError(ValueError):
    """Raised when PF3 fleet placement/recovery invariants are violated."""


class FleetState(StrEnum):
    PENDING = "pending"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    RECONCILE_REQUIRED = "reconcile_required"
    FAILED = "failed"


class DeploymentExecutionPort(Protocol):
    def submit(self, spec: DeploymentExecutionSpec) -> DeploymentExecutionRecord: ...
    def prepare(self, operation_id: str) -> DeploymentExecutionRecord: ...
    def complete(self, operation_id: str) -> DeploymentExecutionRecord: ...
    def retry(self, operation_id: str) -> DeploymentExecutionRecord: ...
    def reconcile(self, operation_id: str) -> DeploymentExecutionRecord: ...
    def get(self, operation_id: str) -> DeploymentExecutionRecord: ...


@dataclass(frozen=True, slots=True)
class ServiceFleetSpec:
    service_id: str
    wave: int
    replicas: tuple[DeploymentExecutionSpec, ...]
    min_healthy_replicas: int
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.service_id.strip():
            raise DeploymentFleetError("fleet service identity must not be empty")
        if self.wave < 0:
            raise DeploymentFleetError("fleet wave must not be negative")
        if not self.replicas:
            raise DeploymentFleetError("fleet service must contain at least one replica")
        if not 1 <= self.min_healthy_replicas <= len(self.replicas):
            raise DeploymentFleetError("minimum healthy replicas must fit replica count")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise DeploymentFleetError("fleet dependencies must not contain duplicates")
        if self.service_id in self.depends_on:
            raise DeploymentFleetError("fleet service must not depend on itself")

        project_ids = {replica.intent.project_id for replica in self.replicas}
        environments = {
            (
                replica.intent.environment.environment_id,
                replica.intent.environment.tier,
                replica.intent.environment.provider_ref,
            )
            for replica in self.replicas
        }
        releases = {replica.intent.release for replica in self.replicas}
        operation_ids = [replica.operation_id for replica in self.replicas]
        work_ids = [replica.request.work_id for replica in self.replicas]
        intent_ids = [replica.intent.intent_id for replica in self.replicas]
        if len(project_ids) != 1 or len(environments) != 1 or len(releases) != 1:
            raise DeploymentFleetError(
                "all replicas must share exact project, environment and release identity"
            )
        if len(operation_ids) != len(set(operation_ids)):
            raise DeploymentFleetError("fleet replicas require unique operation identities")
        if len(work_ids) != len(set(work_ids)):
            raise DeploymentFleetError("fleet replicas require unique work identities")
        if len(intent_ids) != len(set(intent_ids)):
            raise DeploymentFleetError("fleet replicas require unique deployment intents")
        if any(
            replica.request.project_id != replica.intent.project_id
            for replica in self.replicas
        ):
            raise DeploymentFleetError("fleet replica request and intent project mismatch")

    @property
    def project_id(self) -> str:
        return self.replicas[0].intent.project_id

    @property
    def release(self) -> ReleaseRef:
        return self.replicas[0].intent.release

    @property
    def environment_id(self) -> str:
        return self.replicas[0].intent.environment.environment_id

    @property
    def environment_tier(self) -> EnvironmentTier:
        return self.replicas[0].intent.environment.tier


@dataclass(frozen=True, slots=True)
class DeploymentFleetPlan:
    plan_id: str
    project_id: str
    services: tuple[ServiceFleetSpec, ...]

    def __post_init__(self) -> None:
        if not self.plan_id.strip() or not self.project_id.strip():
            raise DeploymentFleetError("fleet plan identity must not be empty")
        if not self.services:
            raise DeploymentFleetError("fleet plan must contain services")
        service_ids = [service.service_id for service in self.services]
        if len(service_ids) != len(set(service_ids)):
            raise DeploymentFleetError("fleet plan contains duplicate service identities")
        by_id = {service.service_id: service for service in self.services}

        operation_ids: list[str] = []
        work_ids: list[str] = []
        intent_ids: list[str] = []
        for service in self.services:
            if service.project_id != self.project_id:
                raise DeploymentFleetError("fleet service belongs to another project")
            unknown = set(service.depends_on) - by_id.keys()
            if unknown:
                raise DeploymentFleetError("fleet dependency references an unknown service")
            if any(by_id[parent].wave >= service.wave for parent in service.depends_on):
                raise DeploymentFleetError(
                    "fleet dependencies must complete in an earlier wave"
                )
            operation_ids.extend(replica.operation_id for replica in service.replicas)
            work_ids.extend(replica.request.work_id for replica in service.replicas)
            intent_ids.extend(replica.intent.intent_id for replica in service.replicas)

        if len(operation_ids) != len(set(operation_ids)):
            raise DeploymentFleetError("fleet plan contains duplicate operation identities")
        if len(work_ids) != len(set(work_ids)):
            raise DeploymentFleetError("fleet plan contains duplicate work identities")
        if len(intent_ids) != len(set(intent_ids)):
            raise DeploymentFleetError("fleet plan contains duplicate deployment intents")


@dataclass(frozen=True, slots=True)
class ReplicaFleetRecord:
    operation_id: str
    work_id: str
    state: OperationState
    node_id: str | None
    attempt: int
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ServiceFleetRecord:
    service_id: str
    release_sha: str
    artifact_digest: str
    environment_id: str
    state: FleetState
    healthy_replicas: int
    required_healthy_replicas: int
    replicas: tuple[ReplicaFleetRecord, ...]


@dataclass(frozen=True, slots=True)
class DeploymentFleetRecord:
    plan: DeploymentFleetPlan
    state: FleetState
    services: tuple[ServiceFleetRecord, ...]


@dataclass(frozen=True, slots=True)
class DeploymentFleetSnapshot:
    plans: tuple[DeploymentFleetPlan, ...]


@dataclass(slots=True)
class DeploymentFleetCoordinator:
    execution: DeploymentExecutionPort
    _plans: dict[str, DeploymentFleetPlan] = field(default_factory=dict, init=False, repr=False)

    def submit(self, plan: DeploymentFleetPlan) -> DeploymentFleetRecord:
        existing = self._plans.get(plan.plan_id)
        if existing is not None and existing != plan:
            raise DeploymentFleetError("fleet plan id conflicts with prior payload")
        if existing is not None:
            return self._summarize(existing)

        for service in plan.services:
            for replica in service.replicas:
                self.execution.submit(replica)
        self._plans[plan.plan_id] = plan
        return self._summarize(plan)

    def advance(self, plan_id: str) -> DeploymentFleetRecord:
        plan = self._plan(plan_id)
        summary = self._summarize(plan)
        by_service = {record.service_id: record for record in summary.services}

        eligible: list[ServiceFleetSpec] = []
        for service in sorted(plan.services, key=lambda item: (item.wave, item.service_id)):
            if all(by_service[parent].state is FleetState.HEALTHY for parent in service.depends_on):
                eligible.append(service)

        # Reserve capacity for every runnable replica before provider completion.
        # ExecutionNodeRegistry grants at most one active lease per node, so this
        # phase deterministically spreads concurrently prepared replicas.
        for service in eligible:
            for replica in service.replicas:
                record = self.execution.get(replica.operation_id)
                if record.state is OperationState.PENDING:
                    self.execution.prepare(replica.operation_id)
                elif record.state in {
                    OperationState.WAITING_FOR_NODE,
                    OperationState.BLOCKED_CREDENTIAL,
                    OperationState.RECOVERY_REQUIRED,
                }:
                    self.execution.retry(replica.operation_id)

        for service in eligible:
            for replica in service.replicas:
                record = self.execution.get(replica.operation_id)
                if record.state is OperationState.PREPARED:
                    self.execution.complete(replica.operation_id)
                elif record.state is OperationState.RECONCILE_REQUIRED:
                    self.execution.reconcile(replica.operation_id)

        return self._summarize(plan)

    def get(self, plan_id: str) -> DeploymentFleetRecord:
        return self._summarize(self._plan(plan_id))

    def snapshot(self) -> DeploymentFleetSnapshot:
        return DeploymentFleetSnapshot(
            tuple(self._plans[key] for key in sorted(self._plans))
        )

    def restore(self, snapshot: DeploymentFleetSnapshot) -> None:
        plan_ids = [plan.plan_id for plan in snapshot.plans]
        if len(plan_ids) != len(set(plan_ids)):
            raise DeploymentFleetError("fleet snapshot contains duplicate plans")
        for plan in snapshot.plans:
            for service in plan.services:
                for replica in service.replicas:
                    record = self.execution.get(replica.operation_id)
                    if record.spec != replica:
                        raise DeploymentFleetError(
                            "fleet snapshot operation disagrees with execution state"
                        )
        self._plans = {plan.plan_id: plan for plan in snapshot.plans}

    def _plan(self, plan_id: str) -> DeploymentFleetPlan:
        plan = self._plans.get(plan_id)
        if plan is None:
            raise DeploymentFleetError("unknown fleet plan")
        return plan

    def _summarize(self, plan: DeploymentFleetPlan) -> DeploymentFleetRecord:
        service_records: list[ServiceFleetRecord] = []
        for service in sorted(plan.services, key=lambda item: (item.wave, item.service_id)):
            replicas = tuple(
                self._replica_record(self.execution.get(spec.operation_id))
                for spec in service.replicas
            )
            healthy = sum(replica.state is OperationState.SUCCEEDED for replica in replicas)
            service_records.append(
                ServiceFleetRecord(
                    service.service_id,
                    service.release.source_sha,
                    service.release.artifact_digest,
                    service.environment_id,
                    _service_state(replicas, healthy, service.min_healthy_replicas),
                    healthy,
                    service.min_healthy_replicas,
                    replicas,
                )
            )
        return DeploymentFleetRecord(
            plan,
            _fleet_state(tuple(service_records)),
            tuple(service_records),
        )

    @staticmethod
    def _replica_record(record: DeploymentExecutionRecord) -> ReplicaFleetRecord:
        return ReplicaFleetRecord(
            record.spec.operation_id,
            record.spec.request.work_id,
            record.state,
            record.node_id,
            record.attempt,
            record.evidence_refs,
        )


def _service_state(
    replicas: tuple[ReplicaFleetRecord, ...],
    healthy: int,
    required: int,
) -> FleetState:
    states = {replica.state for replica in replicas}
    if healthy == len(replicas):
        return FleetState.HEALTHY
    if healthy >= required:
        return FleetState.DEGRADED
    if OperationState.RECONCILE_REQUIRED in states:
        return FleetState.RECONCILE_REQUIRED
    if states & {
        OperationState.PREPARED,
        OperationState.WAITING_FOR_NODE,
        OperationState.BLOCKED_CREDENTIAL,
        OperationState.RECOVERY_REQUIRED,
    }:
        return FleetState.BLOCKED
    if states & {OperationState.REJECTED, OperationState.ROLLED_BACK}:
        return FleetState.FAILED
    return FleetState.PENDING


def _fleet_state(services: tuple[ServiceFleetRecord, ...]) -> FleetState:
    states = {service.state for service in services}
    if states == {FleetState.HEALTHY}:
        return FleetState.HEALTHY
    if FleetState.RECONCILE_REQUIRED in states:
        return FleetState.RECONCILE_REQUIRED
    if FleetState.FAILED in states:
        return FleetState.FAILED
    if FleetState.DEGRADED in states:
        return FleetState.DEGRADED
    if FleetState.BLOCKED in states:
        return FleetState.BLOCKED
    return FleetState.PENDING
