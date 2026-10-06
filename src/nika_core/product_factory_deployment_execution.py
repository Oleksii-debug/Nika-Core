from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from nika_core.product_factory_credentials import CredentialBroker, CredentialBrokerError
from nika_core.product_factory_deployment import (
    DeploymentFabric,
    DeploymentFabricError,
    DeploymentIntent,
    DeploymentRecord,
    DeploymentState,
    ExecutionNodeRegistry,
    ExecutionRequest,
    WorkLease,
)


class DeploymentExecutionError(ValueError):
    """Raised when PF3 execution-mediated deployment invariants are violated."""


class OperationState(StrEnum):
    PENDING = "pending"
    PREPARED = "prepared"
    WAITING_FOR_NODE = "waiting_for_node"
    BLOCKED_CREDENTIAL = "blocked_credential"
    RECONCILE_REQUIRED = "reconcile_required"
    RECOVERY_REQUIRED = "recovery_required"
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"


class ExecutionNodeHealthPort(Protocol):
    def is_available(self, node_id: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class DeploymentExecutionSpec:
    operation_id: str
    request: ExecutionRequest
    intent: DeploymentIntent
    credential_ref: str
    credential_audience: str
    credential_scope: str
    credential_ttl_seconds: int = 300
    node_lease_seconds: int = 300

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.operation_id,
                self.credential_ref,
                self.credential_audience,
                self.credential_scope,
            )
        ):
            raise DeploymentExecutionError("deployment execution identity must not be empty")
        if self.request.project_id != self.intent.project_id:
            raise DeploymentExecutionError("execution request and deployment intent project mismatch")
        if self.credential_ttl_seconds <= 0 or self.node_lease_seconds <= 0:
            raise DeploymentExecutionError("lease durations must be positive")


@dataclass(frozen=True, slots=True)
class DeploymentExecutionRecord:
    spec: DeploymentExecutionSpec
    state: OperationState
    node_id: str | None = None
    deployment_state: DeploymentState | None = None
    evidence_refs: tuple[str, ...] = ()
    attempt: int = 0
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.attempt < 0:
            raise DeploymentExecutionError("deployment execution attempt must not be negative")
        _aware(self.updated_at)
        if self.node_id is not None and not self.node_id.strip():
            raise DeploymentExecutionError("deployment execution node identity must not be empty")


@dataclass(frozen=True, slots=True)
class DeploymentExecutionSnapshot:
    records: tuple[DeploymentExecutionRecord, ...]


@dataclass(slots=True)
class DeploymentExecutionCoordinator:
    nodes: ExecutionNodeRegistry
    credentials: CredentialBroker
    deployments: DeploymentFabric
    node_health: ExecutionNodeHealthPort
    _records: dict[str, DeploymentExecutionRecord] = field(default_factory=dict, init=False, repr=False)
    _node_leases: dict[str, WorkLease] = field(default_factory=dict, init=False, repr=False)
    _credential_leases: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def submit(self, spec: DeploymentExecutionSpec, *, now: datetime | None = None) -> DeploymentExecutionRecord:
        existing = self._records.get(spec.operation_id)
        if existing is not None:
            if existing.spec != spec:
                raise DeploymentExecutionError("operation id conflicts with prior deployment payload")
            return existing
        instant = _aware(now or datetime.now(UTC))
        record = DeploymentExecutionRecord(spec, OperationState.PENDING, updated_at=instant)
        self._records[spec.operation_id] = record
        return record

    def prepare(self, operation_id: str, *, now: datetime | None = None) -> DeploymentExecutionRecord:
        instant = _aware(now or datetime.now(UTC))
        record = self._record(operation_id)
        if record.state in {
            OperationState.SUCCEEDED,
            OperationState.REJECTED,
            OperationState.ROLLED_BACK,
            OperationState.RECONCILE_REQUIRED,
        }:
            return record
        self._release_ephemeral(operation_id)
        try:
            node_lease = self.nodes.acquire(
                record.spec.request,
                now=instant,
                lease_seconds=record.spec.node_lease_seconds,
            )
        except DeploymentFabricError:
            return self._save(
                replace(
                    record,
                    state=OperationState.WAITING_FOR_NODE,
                    node_id=None,
                    attempt=record.attempt + 1,
                    updated_at=instant,
                )
            )
        if not self.node_health.is_available(node_lease.node_id):
            self.nodes.release(node_lease.lease_id)
            return self._save(
                replace(
                    record,
                    state=OperationState.WAITING_FOR_NODE,
                    node_id=None,
                    attempt=record.attempt + 1,
                    updated_at=instant,
                )
            )
        try:
            credential_lease = self.credentials.issue_lease(
                project_id=record.spec.intent.project_id,
                secret_ref=record.spec.credential_ref,
                audience=record.spec.credential_audience,
                scopes=frozenset({record.spec.credential_scope}),
                now=instant,
                ttl_seconds=record.spec.credential_ttl_seconds,
            )
            use = self.credentials.authorize_use(
                lease_id=credential_lease.lease_id,
                project_id=record.spec.intent.project_id,
                scope=record.spec.credential_scope,
                now=instant,
            )
        except CredentialBrokerError:
            self.nodes.release(node_lease.lease_id)
            return self._save(
                replace(
                    record,
                    state=OperationState.BLOCKED_CREDENTIAL,
                    node_id=None,
                    attempt=record.attempt + 1,
                    updated_at=instant,
                )
            )
        self._node_leases[operation_id] = node_lease
        self._credential_leases[operation_id] = credential_lease.lease_id
        evidence = record.evidence_refs + (
            f"execution-node:{node_lease.node_id}",
            f"credential-use:{use.event_id}",
        )
        return self._save(
            replace(
                record,
                state=OperationState.PREPARED,
                node_id=node_lease.node_id,
                evidence_refs=evidence,
                attempt=record.attempt + 1,
                updated_at=instant,
            )
        )

    def complete(self, operation_id: str, *, now: datetime | None = None) -> DeploymentExecutionRecord:
        instant = _aware(now or datetime.now(UTC))
        record = self._record(operation_id)
        if record.state is not OperationState.PREPARED:
            return record
        node_lease = self._node_leases.get(operation_id)
        credential_lease_id = self._credential_leases.get(operation_id)
        if node_lease is None or credential_lease_id is None:
            return self._save(
                replace(record, state=OperationState.RECOVERY_REQUIRED, updated_at=instant)
            )
        if not self.node_health.is_available(node_lease.node_id):
            self._release_ephemeral(operation_id)
            return self._save(
                replace(
                    record,
                    state=OperationState.WAITING_FOR_NODE,
                    node_id=None,
                    updated_at=instant,
                )
            )
        try:
            use = self.credentials.authorize_use(
                lease_id=credential_lease_id,
                project_id=record.spec.intent.project_id,
                scope=record.spec.credential_scope,
                now=instant,
            )
        except CredentialBrokerError:
            self._release_ephemeral(operation_id)
            return self._save(
                replace(
                    record,
                    state=OperationState.BLOCKED_CREDENTIAL,
                    node_id=None,
                    updated_at=instant,
                )
            )
        try:
            deployment = self.deployments.deploy(record.spec.intent)
        finally:
            self._release_ephemeral(operation_id)
        evidence = record.evidence_refs + (f"credential-use:{use.event_id}",) + deployment.provider_evidence_refs
        return self._save(self._from_deployment(record, deployment, evidence, instant))

    def reconcile(self, operation_id: str, *, now: datetime | None = None) -> DeploymentExecutionRecord:
        instant = _aware(now or datetime.now(UTC))
        record = self._record(operation_id)
        if record.state is not OperationState.RECONCILE_REQUIRED:
            return record
        deployment = self.deployments.reconcile(record.spec.intent.intent_id)
        evidence = record.evidence_refs + deployment.provider_evidence_refs
        return self._save(self._from_deployment(record, deployment, evidence, instant))

    def retry(self, operation_id: str, *, now: datetime | None = None) -> DeploymentExecutionRecord:
        record = self._record(operation_id)
        if record.state not in {
            OperationState.WAITING_FOR_NODE,
            OperationState.BLOCKED_CREDENTIAL,
            OperationState.RECOVERY_REQUIRED,
        }:
            return record
        return self.prepare(operation_id, now=now)

    def snapshot(self) -> DeploymentExecutionSnapshot:
        safe_records = []
        for key in sorted(self._records):
            record = self._records[key]
            state = OperationState.RECOVERY_REQUIRED if record.state is OperationState.PREPARED else record.state
            safe_records.append(replace(record, state=state, node_id=None))
        return DeploymentExecutionSnapshot(tuple(safe_records))

    def restore(self, snapshot: DeploymentExecutionSnapshot) -> None:
        operation_ids = [record.spec.operation_id for record in snapshot.records]
        if len(operation_ids) != len(set(operation_ids)):
            raise DeploymentExecutionError("deployment execution snapshot contains duplicate operations")
        restored: dict[str, DeploymentExecutionRecord] = {}
        for record in snapshot.records:
            _aware(record.updated_at)
            if record.state is OperationState.PREPARED or record.node_id is not None:
                raise DeploymentExecutionError("snapshot must not serialize active execution leases")
            restored[record.spec.operation_id] = record
        self._records = restored
        self._node_leases = {}
        self._credential_leases = {}

    def get(self, operation_id: str) -> DeploymentExecutionRecord:
        return self._record(operation_id)

    def _from_deployment(
        self,
        record: DeploymentExecutionRecord,
        deployment: DeploymentRecord,
        evidence_refs: tuple[str, ...],
        updated_at: datetime,
    ) -> DeploymentExecutionRecord:
        state_map = {
            DeploymentState.HEALTHY: OperationState.SUCCEEDED,
            DeploymentState.REJECTED: OperationState.REJECTED,
            DeploymentState.ROLLED_BACK: OperationState.ROLLED_BACK,
            DeploymentState.UNCERTAIN: OperationState.RECONCILE_REQUIRED,
        }
        operation_state = state_map.get(deployment.state)
        if operation_state is None:
            raise DeploymentExecutionError(
                f"deployment fabric returned non-terminal state {deployment.state.value}"
            )
        return replace(
            record,
            state=operation_state,
            deployment_state=deployment.state,
            evidence_refs=evidence_refs,
            updated_at=updated_at,
        )

    def _release_ephemeral(self, operation_id: str) -> None:
        node_lease = self._node_leases.pop(operation_id, None)
        if node_lease is not None:
            try:
                self.nodes.release(node_lease.lease_id)
            except DeploymentFabricError:
                pass
        self._credential_leases.pop(operation_id, None)

    def _record(self, operation_id: str) -> DeploymentExecutionRecord:
        record = self._records.get(operation_id)
        if record is None:
            raise DeploymentExecutionError("unknown deployment execution operation")
        return record

    def _save(self, record: DeploymentExecutionRecord) -> DeploymentExecutionRecord:
        self._records[record.spec.operation_id] = record
        return record


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DeploymentExecutionError("datetime must be timezone-aware")
    return value
