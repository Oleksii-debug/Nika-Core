from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum

from nika_core.product_factory_deployment_execution import (
    DeploymentExecutionCoordinator,
    DeploymentExecutionRecord,
    DeploymentExecutionSnapshot,
    DeploymentExecutionSpec,
    OperationState,
)


class DeploymentWaveError(ValueError):
    """Raised when PF3 multi-service rollout invariants are violated."""


class RolloutState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    PARTIAL_FAILURE = "partial_failure"
    SUCCEEDED = "succeeded"


@dataclass(frozen=True, slots=True)
class ServiceRolloutSpec:
    service_id: str
    wave: int
    execution: DeploymentExecutionSpec
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.service_id) is not str or not self.service_id.strip():
            raise DeploymentWaveError("service identity must be nonempty text")
        if type(self.wave) is not int or self.wave < 0:
            raise DeploymentWaveError("wave index must be a nonnegative integer")
        if type(self.execution) is not DeploymentExecutionSpec:
            raise DeploymentWaveError("service execution must be a deployment execution spec")
        if type(self.depends_on) is not tuple or any(
            type(parent) is not str or not parent.strip() for parent in self.depends_on
        ):
            raise DeploymentWaveError("service dependencies must be a tuple of nonempty text")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise DeploymentWaveError("service dependencies must not contain duplicates")
        if self.service_id in self.depends_on:
            raise DeploymentWaveError("service must not depend on itself")


@dataclass(frozen=True, slots=True)
class DeploymentWavePlan:
    plan_id: str
    project_id: str
    services: tuple[ServiceRolloutSpec, ...]

    def __post_init__(self) -> None:
        if (
            type(self.plan_id) is not str
            or not self.plan_id.strip()
            or type(self.project_id) is not str
            or not self.project_id.strip()
        ):
            raise DeploymentWaveError("rollout identity must be nonempty text")
        if type(self.services) is not tuple or any(
            type(service) is not ServiceRolloutSpec for service in self.services
        ):
            raise DeploymentWaveError("rollout services must be a tuple of service specs")
        if not self.services:
            raise DeploymentWaveError("rollout must contain at least one service")
        service_ids = [service.service_id for service in self.services]
        operation_ids = [service.execution.operation_id for service in self.services]
        if len(service_ids) != len(set(service_ids)):
            raise DeploymentWaveError("rollout contains duplicate service identities")
        if len(operation_ids) != len(set(operation_ids)):
            raise DeploymentWaveError("rollout contains duplicate operation identities")
        known = set(service_ids)
        by_id = {service.service_id: service for service in self.services}
        for service in self.services:
            if service.execution.intent.project_id != self.project_id:
                raise DeploymentWaveError("rollout execution belongs to another project")
            unknown = set(service.depends_on) - known
            if unknown:
                raise DeploymentWaveError("rollout dependency references unknown service")
            if any(by_id[parent].wave >= service.wave for parent in service.depends_on):
                raise DeploymentWaveError("dependencies must be placed in an earlier wave")


@dataclass(frozen=True, slots=True)
class ServiceRolloutRecord:
    service_id: str
    operation_id: str
    wave: int
    state: OperationState
    attempt: int
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DeploymentWaveRecord:
    plan: DeploymentWavePlan
    state: RolloutState
    services: tuple[ServiceRolloutRecord, ...]


@dataclass(frozen=True, slots=True)
class DeploymentWaveSnapshot:
    plans: tuple[DeploymentWaveRecord, ...]
    execution: DeploymentExecutionSnapshot


@dataclass(slots=True)
class DeploymentWaveCoordinator:
    executions: DeploymentExecutionCoordinator
    _plans: dict[str, DeploymentWaveRecord] = field(default_factory=dict, init=False, repr=False)

    def submit(self, plan: DeploymentWavePlan) -> DeploymentWaveRecord:
        existing = self._plans.get(plan.plan_id)
        if existing is not None:
            if existing.plan != plan:
                raise DeploymentWaveError("rollout plan id conflicts with prior payload")
            return existing
        services: list[ServiceRolloutRecord] = []
        for service in sorted(plan.services, key=lambda item: (item.wave, item.service_id)):
            execution = self.executions.submit(service.execution)
            services.append(self._service_record(service, execution))
        record = DeploymentWaveRecord(plan, RolloutState.PENDING, tuple(services))
        self._plans[plan.plan_id] = record
        return record

    def advance(self, plan_id: str) -> DeploymentWaveRecord:
        record = self._record(plan_id)
        if record.state is RolloutState.SUCCEEDED:
            return record

        current = {item.service_id: item for item in record.services}
        specs = {item.service_id: item for item in record.plan.services}
        incomplete_waves = sorted(
            {
                item.wave
                for item in record.services
                if item.state not in _TERMINAL_SUCCESS
                and item.state not in _TERMINAL_FAILURE
            }
        )
        if not incomplete_waves:
            return self._save(self._summarize(record))
        active_wave = incomplete_waves[0]

        for service_id in sorted(
            item.service_id for item in record.services if item.wave == active_wave
        ):
            service_record = current[service_id]
            spec = specs[service_id]
            if service_record.state in _TERMINAL_SUCCESS | _TERMINAL_FAILURE:
                continue
            if not self._dependencies_succeeded(spec, current):
                continue
            execution = self._drive(spec.execution.operation_id)
            current[service_id] = self._service_record(spec, execution)

        updated = replace(
            record,
            services=tuple(
                current[item.service_id]
                for item in sorted(
                    record.services,
                    key=lambda value: (value.wave, value.service_id),
                )
            ),
        )
        return self._save(self._summarize(updated))

    def get(self, plan_id: str) -> DeploymentWaveRecord:
        return self._record(plan_id)

    def snapshot(self) -> DeploymentWaveSnapshot:
        execution = self.executions.snapshot()
        safe = {record.spec.operation_id: record for record in execution.records}
        plans: list[DeploymentWaveRecord] = []
        for key in sorted(self._plans):
            record = self._plans[key]
            services = tuple(
                replace(
                    service,
                    state=safe[service.operation_id].state,
                    attempt=safe[service.operation_id].attempt,
                    evidence_refs=safe[service.operation_id].evidence_refs,
                )
                for service in record.services
            )
            plans.append(self._summarize(replace(record, services=services)))
        return DeploymentWaveSnapshot(tuple(plans), execution)

    def restore(self, snapshot: DeploymentWaveSnapshot) -> None:
        if type(snapshot) is not DeploymentWaveSnapshot:
            raise DeploymentWaveError("invalid rollout snapshot")
        if (
            type(snapshot.plans) is not tuple
            or type(snapshot.execution) is not DeploymentExecutionSnapshot
        ):
            raise DeploymentWaveError("invalid rollout snapshot structure")
        if any(type(record) is not DeploymentWaveRecord for record in snapshot.plans):
            raise DeploymentWaveError("invalid rollout snapshot plan record")

        plan_ids: list[str] = []
        for record in snapshot.plans:
            if (
                type(record.plan) is not DeploymentWavePlan
                or type(record.services) is not tuple
                or type(record.plan.services) is not tuple
                or any(
                    type(service) is not ServiceRolloutSpec
                    for service in record.plan.services
                )
                or any(
                    type(service) is not ServiceRolloutRecord
                    for service in record.services
                )
            ):
                raise DeploymentWaveError("invalid rollout snapshot plan structure")
            try:
                for service in record.plan.services:
                    service.__post_init__()
                record.plan.__post_init__()
            except (DeploymentWaveError, AttributeError, TypeError, ValueError) as exc:
                raise DeploymentWaveError("invalid rollout snapshot plan structure") from exc
            if type(record.plan.plan_id) is not str or not record.plan.plan_id.strip():
                raise DeploymentWaveError("invalid rollout snapshot plan identity")
            plan_ids.append(record.plan.plan_id)
        if len(plan_ids) != len(set(plan_ids)):
            raise DeploymentWaveError("rollout snapshot contains duplicate plans")

        if type(snapshot.execution.records) is not tuple or any(
            type(item) is not DeploymentExecutionRecord
            or type(item.spec) is not DeploymentExecutionSpec
            for item in snapshot.execution.records
        ):
            raise DeploymentWaveError("invalid rollout execution snapshot structure")
        execution_records: dict[str, DeploymentExecutionRecord] = {}
        for item in snapshot.execution.records:
            operation_id = item.spec.operation_id
            if type(operation_id) is not str or not operation_id.strip():
                raise DeploymentWaveError("invalid rollout execution operation identity")
            if operation_id in execution_records:
                raise DeploymentWaveError(
                    "rollout execution snapshot contains duplicate operations"
                )
            execution_records[operation_id] = item

        restored: dict[str, DeploymentWaveRecord] = {}
        for record in snapshot.plans:
            planned = {service.service_id: service for service in record.plan.services}
            expected = {service.execution.operation_id for service in record.plan.services}
            actual = {service.operation_id for service in record.services}
            if expected != actual:
                raise DeploymentWaveError("rollout snapshot operation set is inconsistent")
            if not expected <= execution_records.keys():
                raise DeploymentWaveError("rollout snapshot is missing execution state")
            if len(record.services) != len(record.plan.services):
                raise DeploymentWaveError("rollout snapshot service set is inconsistent")
            for service in record.services:
                if type(service) is not ServiceRolloutRecord:
                    raise DeploymentWaveError("invalid rollout snapshot service record")
                spec = planned.get(service.service_id)
                if (
                    spec is None
                    or service.operation_id != spec.execution.operation_id
                    or type(service.wave) is not int
                    or service.wave != spec.wave
                ):
                    raise DeploymentWaveError("rollout snapshot service identity is inconsistent")
                execution_record = execution_records[service.operation_id]
                if execution_record.spec != spec.execution:
                    raise DeploymentWaveError("rollout snapshot execution specification changed")
                if (
                    type(service.state) is not OperationState
                    or service.state is not execution_record.state
                    or type(service.attempt) is not int
                    or service.attempt != execution_record.attempt
                    or service.evidence_refs != execution_record.evidence_refs
                ):
                    raise DeploymentWaveError("rollout snapshot disagrees with execution snapshot")
            if record.state is not self._summarize(record).state:
                raise DeploymentWaveError("rollout snapshot summary is inconsistent")
            restored[record.plan.plan_id] = record

        self.executions.restore(snapshot.execution)
        self._plans = restored

    def _drive(self, operation_id: str) -> DeploymentExecutionRecord:
        record = self.executions.get(operation_id)
        if record.state is OperationState.PENDING:
            record = self.executions.prepare(operation_id)
        elif record.state in {
            OperationState.WAITING_FOR_NODE,
            OperationState.BLOCKED_CREDENTIAL,
            OperationState.RECOVERY_REQUIRED,
        }:
            record = self.executions.retry(operation_id)

        if record.state is OperationState.PREPARED:
            record = self.executions.complete(operation_id)
        if record.state is OperationState.RECONCILE_REQUIRED:
            record = self.executions.reconcile(operation_id)
        return record

    @staticmethod
    def _dependencies_succeeded(
        spec: ServiceRolloutSpec,
        records: dict[str, ServiceRolloutRecord],
    ) -> bool:
        return all(records[parent].state is OperationState.SUCCEEDED for parent in spec.depends_on)

    @staticmethod
    def _service_record(
        spec: ServiceRolloutSpec,
        execution: DeploymentExecutionRecord,
    ) -> ServiceRolloutRecord:
        return ServiceRolloutRecord(
            spec.service_id,
            execution.spec.operation_id,
            spec.wave,
            execution.state,
            execution.attempt,
            execution.evidence_refs,
        )

    @staticmethod
    def _summarize(record: DeploymentWaveRecord) -> DeploymentWaveRecord:
        states = {item.state for item in record.services}
        if states <= _TERMINAL_SUCCESS:
            state = RolloutState.SUCCEEDED
        elif states & _TERMINAL_FAILURE:
            state = RolloutState.PARTIAL_FAILURE
        elif states & {
            OperationState.WAITING_FOR_NODE,
            OperationState.BLOCKED_CREDENTIAL,
            OperationState.RECOVERY_REQUIRED,
            OperationState.RECONCILE_REQUIRED,
        }:
            state = RolloutState.PAUSED
        elif states == {OperationState.PENDING}:
            state = RolloutState.PENDING
        else:
            state = RolloutState.RUNNING
        return replace(record, state=state)

    def _record(self, plan_id: str) -> DeploymentWaveRecord:
        record = self._plans.get(plan_id)
        if record is None:
            raise DeploymentWaveError("unknown rollout plan")
        return record

    def _save(self, record: DeploymentWaveRecord) -> DeploymentWaveRecord:
        self._plans[record.plan.plan_id] = record
        return record


_TERMINAL_SUCCESS = {OperationState.SUCCEEDED}
_TERMINAL_FAILURE = {OperationState.REJECTED, OperationState.ROLLED_BACK}
