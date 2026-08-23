from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from nika_core.product_factory_coordinator import (
    CoordinatorError,
    WorkState,
    validate_trusted_plan_snapshot,
)
from nika_core.product_factory_deployment import (
    DeploymentFabricError,
    ExecutionNode,
    ExecutionNodeRegistry,
    ExecutionRequest,
    WorkLease,
)
from nika_core.product_factory_deployment_fleet import DeploymentFleetRecord, FleetState
from nika_core.product_factory_incidents import TrustedReviewAuthority
from nika_core.product_factory_operations import ProductOperationsCoordinator, ServiceRecord
from nika_core.product_factory_operations_contracts import ServiceHealth

FLEET_REPLACEMENT_SCHEMA = "nika-pf3-fleet-replacement-v2"


class FleetReplacementError(ValueError):
    """Raised when PF3 fleet replacement/rebalancing invariants are violated."""


class ReplicaReplacementState(StrEnum):
    PENDING = "pending"
    WAITING_FOR_SOURCE_LEASE = "waiting_for_source_lease"
    WAITING_FOR_ORPHAN_LEASE = "waiting_for_orphan_lease"
    WAITING_FOR_CAPACITY = "waiting_for_capacity"
    BLOCKED_BUDGET = "blocked_budget"
    BLOCKED_CREDENTIAL = "blocked_credential"
    DISPATCHING = "dispatching"
    RECONCILE_REQUIRED = "reconcile_required"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class FleetReplacementState(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    RECONCILE_REQUIRED = "reconcile_required"
    PARTIAL_FAILURE = "partial_failure"
    SUCCEEDED = "succeeded"


@dataclass(frozen=True, slots=True)
class EnvironmentReplacementBudget:
    environment_id: str
    max_unavailable_replicas: int
    max_concurrent_replacements: int

    def __post_init__(self) -> None:
        if not self.environment_id.strip():
            raise FleetReplacementError("replacement environment identity must not be empty")
        if self.max_unavailable_replicas < 0 or self.max_concurrent_replacements <= 0:
            raise FleetReplacementError("replacement environment budget is invalid")


@dataclass(frozen=True, slots=True)
class ReplicaReplacementSpec:
    service_id: str
    replica_id: str
    deployment_operation_id: str
    source_node_id: str
    request: ExecutionRequest

    def __post_init__(self) -> None:
        identities = (
            self.service_id,
            self.replica_id,
            self.deployment_operation_id,
            self.source_node_id,
        )
        if not all(value.strip() for value in identities):
            raise FleetReplacementError("replacement binding identity must not be empty")


@dataclass(frozen=True, slots=True)
class FleetReplacementPlan:
    plan_id: str
    project_id: str
    fleet_plan_id: str
    replacements: tuple[ReplicaReplacementSpec, ...]
    budgets: tuple[EnvironmentReplacementBudget, ...]
    max_replicas_per_node: int
    authorization_work_id: str
    approval_ref: str
    reason: str
    evidence_refs: tuple[str, ...]
    retain_cordoned_nodes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        identities = (
            self.plan_id,
            self.project_id,
            self.fleet_plan_id,
            self.authorization_work_id,
            self.approval_ref,
            self.reason,
        )
        if not all(value.strip() for value in identities):
            raise FleetReplacementError("replacement plan identity/authorization must not be empty")
        if not self.replacements or not self.budgets or not self.evidence_refs:
            raise FleetReplacementError("replacement plan scope/evidence must not be empty")
        if self.max_replicas_per_node <= 0:
            raise FleetReplacementError("replacement node placement limit must be positive")

        keys = [(item.service_id, item.replica_id) for item in self.replacements]
        operation_ids = [item.deployment_operation_id for item in self.replacements]
        work_ids = [item.request.work_id for item in self.replacements]
        budget_ids = [item.environment_id for item in self.budgets]
        if len(keys) != len(set(keys)):
            raise FleetReplacementError("replacement plan contains duplicate replica bindings")
        if len(operation_ids) != len(set(operation_ids)):
            raise FleetReplacementError("replacement plan contains duplicate deployment operations")
        if len(work_ids) != len(set(work_ids)):
            raise FleetReplacementError("replacement plan contains duplicate execution work ids")
        if len(budget_ids) != len(set(budget_ids)):
            raise FleetReplacementError("replacement plan contains duplicate environment budgets")
        if any(item.request.project_id != self.project_id for item in self.replacements):
            raise FleetReplacementError("replacement execution request belongs to another project")
        if (
            len(self.retain_cordoned_nodes) != len(set(self.retain_cordoned_nodes))
            or any(not node_id.strip() for node_id in self.retain_cordoned_nodes)
        ):
            raise FleetReplacementError("retained cordon identities are invalid")


@dataclass(frozen=True, slots=True)
class ReplicaReplacementRequest:
    request_id: str
    plan_id: str
    project_id: str
    fleet_plan_id: str
    environment_id: str
    service_id: str
    replica_id: str
    deployment_operation_id: str
    source_node_id: str
    target_node_id: str
    release_sha: str
    artifact_digest: str
    reason: str
    approval_ref: str
    authorization_work_id: str
    review_ref: str
    plan_fingerprint: str
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        identities = (
            self.request_id,
            self.plan_id,
            self.project_id,
            self.fleet_plan_id,
            self.environment_id,
            self.service_id,
            self.replica_id,
            self.deployment_operation_id,
            self.source_node_id,
            self.target_node_id,
            self.reason,
            self.approval_ref,
            self.authorization_work_id,
            self.review_ref,
        )
        if not all(value.strip() for value in identities) or not self.evidence_refs:
            raise FleetReplacementError("replacement request identity/evidence is invalid")
        _validate_sha(self.release_sha)
        _validate_digest(self.artifact_digest)
        _validate_digest(self.plan_fingerprint)
        if self.source_node_id == self.target_node_id:
            raise FleetReplacementError("replacement target must differ from source node")


@dataclass(frozen=True, slots=True)
class ReplicaReplacementResult:
    applied: bool
    uncertain: bool
    evidence_refs: tuple[str, ...]
    observed_node_id: str | None = None
    release_sha: str | None = None
    artifact_digest: str | None = None
    healthy: bool | None = None

    def __post_init__(self) -> None:
        if not self.evidence_refs or (self.applied and self.uncertain):
            raise FleetReplacementError("replacement provider result is invalid")
        for value in (self.observed_node_id, self.release_sha, self.artifact_digest):
            if value is not None and not value.strip():
                raise FleetReplacementError("replacement provider evidence identity is empty")
        if self.release_sha is not None:
            _validate_sha(self.release_sha)
        if self.artifact_digest is not None:
            _validate_digest(self.artifact_digest)


class ReplicaReplacementPort(Protocol):
    def apply(self, request: ReplicaReplacementRequest) -> ReplicaReplacementResult: ...
    def inspect(self, request: ReplicaReplacementRequest) -> ReplicaReplacementResult: ...


class FleetViewPort(Protocol):
    def get(self, plan_id: str) -> DeploymentFleetRecord: ...


@dataclass(frozen=True, slots=True)
class ReplicaReplacementBinding:
    spec: ReplicaReplacementSpec
    environment_id: str
    release_sha: str
    artifact_digest: str


@dataclass(frozen=True, slots=True)
class ReplicaReplacementRecord:
    binding: ReplicaReplacementBinding
    state: ReplicaReplacementState = ReplicaReplacementState.PENDING
    attempt: int = 0
    target_node_id: str | None = None
    evidence_refs: tuple[str, ...] = ()
    pending_request: ReplicaReplacementRequest | None = None
    source_was_enabled: bool | None = None

    def __post_init__(self) -> None:
        if self.attempt < 0:
            raise FleetReplacementError("replacement attempt counter is invalid")
        if self.pending_request is not None:
            if self.state not in {
                ReplicaReplacementState.DISPATCHING,
                ReplicaReplacementState.RECONCILE_REQUIRED,
            }:
                raise FleetReplacementError("pending provider request requires recovery state")
            if self.pending_request.replica_id != self.binding.spec.replica_id:
                raise FleetReplacementError("pending provider request belongs to another replica")
            if self.target_node_id != self.pending_request.target_node_id:
                raise FleetReplacementError("pending provider request target is inconsistent")
        elif self.state in {
            ReplicaReplacementState.DISPATCHING,
            ReplicaReplacementState.RECONCILE_REQUIRED,
        }:
            raise FleetReplacementError("provider recovery state requires an exact pending request")
        if self.state is ReplicaReplacementState.SUCCEEDED and self.target_node_id is None:
            raise FleetReplacementError("successful replacement requires target node provenance")


@dataclass(frozen=True, slots=True)
class FleetReplacementRecord:
    plan: FleetReplacementPlan
    state: FleetReplacementState
    replacements: tuple[ReplicaReplacementRecord, ...]


@dataclass(frozen=True, slots=True)
class FleetReplacementSnapshot:
    plans: tuple[FleetReplacementPlan, ...]
    records: tuple[tuple[str, tuple[ReplicaReplacementRecord, ...]], ...]


_ACTIVE_STATES = {
    ReplicaReplacementState.DISPATCHING,
    ReplicaReplacementState.RECONCILE_REQUIRED,
}
_BLOCKED_STATES = {
    ReplicaReplacementState.WAITING_FOR_SOURCE_LEASE,
    ReplicaReplacementState.WAITING_FOR_ORPHAN_LEASE,
    ReplicaReplacementState.WAITING_FOR_CAPACITY,
    ReplicaReplacementState.BLOCKED_BUDGET,
    ReplicaReplacementState.BLOCKED_CREDENTIAL,
}


@dataclass(slots=True)
class FleetReplacementCoordinator:
    fleet: FleetViewPort
    operations: ProductOperationsCoordinator
    nodes: ExecutionNodeRegistry
    port: ReplicaReplacementPort
    _plans: dict[str, FleetReplacementPlan] = field(default_factory=dict, init=False, repr=False)
    _records: dict[str, dict[tuple[str, str], ReplicaReplacementRecord]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _authorities: dict[str, TrustedReviewAuthority] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _review_refs: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def submit(
        self,
        plan: FleetReplacementPlan,
        *,
        review_authority: TrustedReviewAuthority,
        review_ref: str,
    ) -> FleetReplacementRecord:
        self._validate_authority(plan, review_authority, review_ref)
        existing = self._plans.get(plan.plan_id)
        if existing is not None:
            if existing != plan:
                raise FleetReplacementError("replacement plan id conflicts with prior payload")
            if (
                self._authorities.get(plan.plan_id) != review_authority
                or self._review_refs.get(plan.plan_id) != review_ref
            ):
                raise FleetReplacementError(
                    "replacement plan authority conflicts with prior submit"
                )
            return self.get(plan.plan_id)
        self._validate_plan(plan)
        records: dict[tuple[str, str], ReplicaReplacementRecord] = {}
        for spec in plan.replacements:
            binding = self._bind(plan, spec)
            records[(spec.service_id, spec.replica_id)] = ReplicaReplacementRecord(binding)
        self._plans[plan.plan_id] = plan
        self._records[plan.plan_id] = records
        self._authorities[plan.plan_id] = review_authority
        self._review_refs[plan.plan_id] = review_ref
        return self.get(plan.plan_id)

    def advance(
        self,
        plan_id: str,
        *,
        now: datetime | None = None,
        lease_seconds: int = 300,
    ) -> FleetReplacementRecord:
        if lease_seconds <= 0:
            raise FleetReplacementError("replacement lease duration must be positive")
        instant = _aware(now or datetime.now(UTC))
        plan = self._plan(plan_id)
        authority, review_ref = self._authority(plan_id)
        self._validate_authority(plan, authority, review_ref)
        records = self._records[plan_id]
        self._cleanup_terminal_leases(records)

        for key in self._ordered_keys(plan):
            record = records[key]
            if record.state in {
                ReplicaReplacementState.SUCCEEDED,
                ReplicaReplacementState.FAILED,
            }:
                continue
            if record.state in _ACTIVE_STATES:
                records[key] = self._reconcile(plan, record)
                self._maybe_resume_source(plan, records[key])
                return self.get(plan_id)

        for key in self._ordered_keys(plan):
            record = records[key]
            if record.state in {
                ReplicaReplacementState.SUCCEEDED,
                ReplicaReplacementState.FAILED,
            }:
                continue
            spec = record.binding.spec
            self._validate_live_binding(plan, record.binding)
            service = self._service(spec.service_id)
            if service.blocked_credentials or service.health is ServiceHealth.BLOCKED:
                records[key] = replace(record, state=ReplicaReplacementState.BLOCKED_CREDENTIAL)
                continue
            if not self._budget_allows(plan, records, record):
                records[key] = replace(record, state=ReplicaReplacementState.BLOCKED_BUDGET)
                continue
            orphan = self._matching_lease(plan.project_id, spec.request.work_id)
            if orphan is not None and orphan.expires_at > instant:
                records[key] = replace(
                    record,
                    state=ReplicaReplacementState.WAITING_FOR_ORPHAN_LEASE,
                )
                continue

            source_enabled = self._cordon_source(spec.source_node_id, record.source_was_enabled)
            record = replace(record, source_was_enabled=source_enabled)
            records[key] = record
            if self._source_has_other_active_lease(spec.source_node_id, spec.request.work_id):
                records[key] = replace(
                    record,
                    state=ReplicaReplacementState.WAITING_FOR_SOURCE_LEASE,
                )
                continue

            try:
                lease = self._acquire_target(plan, records, record, instant, lease_seconds)
            except DeploymentFabricError as exc:
                if "no available execution node" not in str(exc):
                    raise
                records[key] = replace(record, state=ReplicaReplacementState.WAITING_FOR_CAPACITY)
                self._maybe_resume_source(plan, record)
                continue

            request = self._request(plan, record, lease, review_ref)
            checkpoint = replace(
                record,
                state=ReplicaReplacementState.DISPATCHING,
                attempt=record.attempt + 1,
                target_node_id=lease.node_id,
                pending_request=request,
                evidence_refs=record.evidence_refs + (f"lease:{lease.node_id}",),
            )
            records[key] = checkpoint
            try:
                result = self.port.apply(request)
            except Exception:  # noqa: BLE001 - external side-effect boundary must reconcile.
                records[key] = replace(
                    checkpoint,
                    state=ReplicaReplacementState.RECONCILE_REQUIRED,
                )
                raise FleetReplacementError(
                    "replacement provider call failed; exact inspection is required"
                ) from None
            try:
                records[key] = self._consume_result(checkpoint, request, result)
            except FleetReplacementError:
                records[key] = replace(
                    checkpoint,
                    state=ReplicaReplacementState.RECONCILE_REQUIRED,
                )
                raise
            self._maybe_resume_source(plan, records[key])
            return self.get(plan_id)

        return self.get(plan_id)

    def get(self, plan_id: str) -> FleetReplacementRecord:
        plan = self._plan(plan_id)
        records = tuple(self._records[plan_id][key] for key in self._ordered_keys(plan))
        return FleetReplacementRecord(plan, _fleet_state(records), records)

    def snapshot(self) -> FleetReplacementSnapshot:
        return FleetReplacementSnapshot(
            tuple(self._plans[key] for key in sorted(self._plans)),
            tuple(
                (
                    plan_id,
                    tuple(
                        self._records[plan_id][key]
                        for key in self._ordered_keys(self._plans[plan_id])
                    ),
                )
                for plan_id in sorted(self._plans)
            ),
        )

    def restore(
        self,
        snapshot: FleetReplacementSnapshot,
        *,
        review_authorities: tuple[tuple[TrustedReviewAuthority, str], ...],
    ) -> None:
        plan_ids = [plan.plan_id for plan in snapshot.plans]
        record_plan_ids = [plan_id for plan_id, _ in snapshot.records]
        if (
            len(plan_ids) != len(set(plan_ids))
            or len(record_plan_ids) != len(set(record_plan_ids))
            or set(plan_ids) != set(record_plan_ids)
        ):
            raise FleetReplacementError("replacement snapshot plan identities are invalid")
        if not snapshot.plans and review_authorities:
            raise FleetReplacementError("replacement restore authority has no snapshot plan")
        if snapshot.plans and not review_authorities:
            raise FleetReplacementError(
                "replacement snapshot requires independent trusted review authority"
            )

        plans = {plan.plan_id: plan for plan in snapshot.plans}
        authorities: dict[str, TrustedReviewAuthority] = {}
        review_refs: dict[str, str] = {}
        for plan in snapshot.plans:
            matches = [
                (authority, review_ref)
                for authority, review_ref in review_authorities
                if self._authority_matches(plan, authority, review_ref)
            ]
            if len(matches) != 1:
                raise FleetReplacementError(
                    "replacement snapshot requires exactly one matching review authority"
                )
            authority, review_ref = matches[0]
            self._validate_authority(plan, authority, review_ref)
            authorities[plan.plan_id] = authority
            review_refs[plan.plan_id] = review_ref

        records: dict[str, dict[tuple[str, str], ReplicaReplacementRecord]] = {}
        for plan_id, saved in snapshot.records:
            plan = plans[plan_id]
            self._validate_plan(plan)
            expected = [(item.service_id, item.replica_id) for item in plan.replacements]
            actual = [
                (record.binding.spec.service_id, record.binding.spec.replica_id)
                for record in saved
            ]
            if actual != expected:
                raise FleetReplacementError("replacement snapshot record order/scope is invalid")
            mapped: dict[tuple[str, str], ReplicaReplacementRecord] = {}
            for record in saved:
                live = self._bind(plan, record.binding.spec)
                if live != record.binding:
                    raise FleetReplacementError(
                        "replacement snapshot disagrees with exact live placement provenance"
                    )
                normalized = record
                if record.state is ReplicaReplacementState.DISPATCHING:
                    normalized = replace(
                        record,
                        state=ReplicaReplacementState.RECONCILE_REQUIRED,
                    )
                mapped[(
                    record.binding.spec.service_id,
                    record.binding.spec.replica_id,
                )] = normalized
            records[plan_id] = mapped

        self._plans = plans
        self._records = records
        self._authorities = authorities
        self._review_refs = review_refs
        for plan_id, mapped in records.items():
            self._validate_pending_leases(self._plans[plan_id], mapped)
            self._cleanup_terminal_leases(mapped)

    def _validate_authority(
        self,
        plan: FleetReplacementPlan,
        authority: TrustedReviewAuthority,
        review_ref: str,
    ) -> None:
        if not isinstance(authority, TrustedReviewAuthority) or not review_ref.strip():
            raise FleetReplacementError(
                "replacement side effect requires external trusted review authority"
            )
        try:
            validate_trusted_plan_snapshot(
                authority.snapshot,
                authority.trusted_plan_fingerprint,
            )
        except CoordinatorError as exc:
            raise FleetReplacementError(
                "replacement review authority does not match external trusted plan"
            ) from exc
        if authority.snapshot.project_id != plan.project_id:
            raise FleetReplacementError("replacement review authority belongs to another project")
        matches = [
            item
            for item in authority.snapshot.records
            if item.request.work_id == plan.authorization_work_id
        ]
        if len(matches) != 1:
            raise FleetReplacementError(
                "replacement plan requires exactly one matching authorization work record"
            )
        work = matches[0]
        if (
            work.state is not WorkState.ACCEPTED
            or work.result is None
            or work.review is None
            or not work.review.accepted
            or not work.result.coding_result.succeeded
            or not work.result.coding_result.test_evidence
            or any(item.exit_code != 0 for item in work.result.coding_result.test_evidence)
        ):
            raise FleetReplacementError(
                "replacement authorization work lacks accepted reviewed test evidence"
            )
        if review_ref not in work.review.evidence_refs:
            raise FleetReplacementError(
                "replacement review ref is absent from independent review evidence"
            )
        binding_ref = _authority_binding_ref(plan)
        if binding_ref not in work.review.evidence_refs:
            raise FleetReplacementError(
                "replacement plan fingerprint is absent from independent review evidence"
            )

    def _authority_matches(
        self,
        plan: FleetReplacementPlan,
        authority: TrustedReviewAuthority,
        review_ref: str,
    ) -> bool:
        if not isinstance(authority, TrustedReviewAuthority) or not review_ref.strip():
            return False
        if authority.snapshot.project_id != plan.project_id:
            return False
        return any(
            item.request.work_id == plan.authorization_work_id
            and item.review is not None
            and review_ref in item.review.evidence_refs
            and _authority_binding_ref(plan) in item.review.evidence_refs
            for item in authority.snapshot.records
        )

    def _authority(self, plan_id: str) -> tuple[TrustedReviewAuthority, str]:
        try:
            return self._authorities[plan_id], self._review_refs[plan_id]
        except KeyError as exc:
            raise FleetReplacementError(
                "replacement plan lost independent review authority"
            ) from exc

    def _validate_plan(self, plan: FleetReplacementPlan) -> None:
        fleet = self.fleet.get(plan.fleet_plan_id)
        if fleet.plan.project_id != plan.project_id:
            raise FleetReplacementError("replacement fleet belongs to another project")
        operations = self.operations.snapshot()
        if operations.project_id != plan.project_id:
            raise FleetReplacementError("replacement operations state belongs to another project")
        environments = {self._bind(plan, spec).environment_id for spec in plan.replacements}
        budgets = {budget.environment_id for budget in plan.budgets}
        if environments != budgets:
            raise FleetReplacementError(
                "replacement plan requires exact environment budget coverage"
            )
        known_nodes = {node.identity.node_id for node in self.nodes.snapshot().nodes}
        sources = {item.source_node_id for item in plan.replacements}
        if not sources <= known_nodes or not set(plan.retain_cordoned_nodes) <= sources:
            raise FleetReplacementError("replacement plan references invalid source node cordons")

    def _bind(
        self,
        plan: FleetReplacementPlan,
        spec: ReplicaReplacementSpec,
    ) -> ReplicaReplacementBinding:
        fleet = self.fleet.get(plan.fleet_plan_id)
        service = next(
            (item for item in fleet.services if item.service_id == spec.service_id),
            None,
        )
        if service is None:
            raise FleetReplacementError("replacement service is missing from fleet")
        if service.state not in {FleetState.HEALTHY, FleetState.DEGRADED}:
            raise FleetReplacementError("replacement requires healthy/degraded fleet service")
        fleet_replica = next(
            (
                item
                for item in service.replicas
                if item.operation_id == spec.deployment_operation_id
            ),
            None,
        )
        if fleet_replica is None or fleet_replica.node_id != spec.source_node_id:
            raise FleetReplacementError("deployment replica source placement does not match plan")
        operations = self._service(spec.service_id)
        ops_replica = next(
            (
                item
                for item in operations.service.replicas
                if item.replica_id == spec.replica_id
            ),
            None,
        )
        if ops_replica is None or ops_replica.node_id != spec.source_node_id:
            raise FleetReplacementError("operations replica source placement does not match plan")
        if (
            operations.service.project_id != plan.project_id
            or operations.service.environment_id != service.environment_id
            or operations.service.release_sha != service.release_sha
        ):
            raise FleetReplacementError("fleet and operations replacement provenance disagree")
        return ReplicaReplacementBinding(
            spec,
            service.environment_id,
            service.release_sha,
            service.artifact_digest,
        )

    def _validate_live_binding(
        self,
        plan: FleetReplacementPlan,
        binding: ReplicaReplacementBinding,
    ) -> None:
        if self._bind(plan, binding.spec) != binding:
            raise FleetReplacementError("replacement live placement/release provenance drifted")

    def _budget_allows(
        self,
        plan: FleetReplacementPlan,
        records: dict[tuple[str, str], ReplicaReplacementRecord],
        record: ReplicaReplacementRecord,
    ) -> bool:
        environment_id = record.binding.environment_id
        budget = next(item for item in plan.budgets if item.environment_id == environment_id)
        active = sum(
            item.state in _ACTIVE_STATES and item.binding.environment_id == environment_id
            for item in records.values()
        )
        if active >= budget.max_concurrent_replacements:
            return False

        placements = self._placements(records)
        operations = self.operations.snapshot()
        unavailable_nodes = set(operations.unavailable_nodes)
        unavailable_environment = 0
        unavailable_service = 0
        service = self._service(record.binding.spec.service_id)
        for service_record in operations.services:
            if service_record.service.environment_id != environment_id:
                continue
            for replica in service_record.service.replicas:
                node_id = placements.get(
                    (service_record.service.service_id, replica.replica_id),
                    replica.node_id,
                )
                if node_id in unavailable_nodes:
                    unavailable_environment += 1
                    if service_record.service.service_id == service.service.service_id:
                        unavailable_service += 1
        for item in records.values():
            if item.state not in _ACTIVE_STATES or item.pending_request is None:
                continue
            if item.binding.environment_id != environment_id:
                continue
            if item.pending_request.source_node_id not in unavailable_nodes:
                unavailable_environment += 1
                if item.binding.spec.service_id == service.service.service_id:
                    unavailable_service += 1

        source_unavailable = record.binding.spec.source_node_id in unavailable_nodes
        if source_unavailable:
            return True
        if unavailable_environment + 1 > budget.max_unavailable_replicas:
            return False
        total = len(service.service.replicas)
        return total - (unavailable_service + 1) >= service.service.min_healthy_replicas

    def _acquire_target(
        self,
        plan: FleetReplacementPlan,
        records: dict[tuple[str, str], ReplicaReplacementRecord],
        record: ReplicaReplacementRecord,
        now: datetime,
        lease_seconds: int,
    ) -> WorkLease:
        placements = self._placements(records)
        counts: dict[str, int] = {}
        for node_id in placements.values():
            counts[node_id] = counts.get(node_id, 0) + 1
        excluded = {
            node_id
            for node_id, count in counts.items()
            if count >= plan.max_replicas_per_node
        }
        service_id = record.binding.spec.service_id
        replica_id = record.binding.spec.replica_id
        excluded.update(
            node_id
            for (candidate_service, candidate_replica), node_id in placements.items()
            if candidate_service == service_id and candidate_replica != replica_id
        )
        excluded.add(record.binding.spec.source_node_id)

        temporarily_disabled: list[ExecutionNode] = []
        for node in self.nodes.snapshot().nodes:
            if node.identity.node_id in excluded and node.enabled:
                if node.identity.node_id == record.binding.spec.source_node_id:
                    continue
                temporarily_disabled.append(node)
                self.nodes.register(replace(node, enabled=False))
        try:
            return self.nodes.acquire(
                record.binding.spec.request,
                now=now,
                lease_seconds=lease_seconds,
            )
        finally:
            for node in temporarily_disabled:
                self.nodes.register(node)

    def _request(
        self,
        plan: FleetReplacementPlan,
        record: ReplicaReplacementRecord,
        lease: WorkLease,
        review_ref: str,
    ) -> ReplicaReplacementRequest:
        binding = record.binding
        spec = binding.spec
        fingerprint = fleet_replacement_plan_fingerprint(plan)
        return ReplicaReplacementRequest(
            request_id=(
                f"{plan.plan_id}:{spec.service_id}:{spec.replica_id}:"
                f"replace:{record.attempt + 1}"
            ),
            plan_id=plan.plan_id,
            project_id=plan.project_id,
            fleet_plan_id=plan.fleet_plan_id,
            environment_id=binding.environment_id,
            service_id=spec.service_id,
            replica_id=spec.replica_id,
            deployment_operation_id=spec.deployment_operation_id,
            source_node_id=spec.source_node_id,
            target_node_id=lease.node_id,
            release_sha=binding.release_sha,
            artifact_digest=binding.artifact_digest,
            reason=plan.reason,
            approval_ref=plan.approval_ref,
            authorization_work_id=plan.authorization_work_id,
            review_ref=review_ref,
            plan_fingerprint=fingerprint,
            evidence_refs=plan.evidence_refs
            + (review_ref, _authority_binding_ref(plan), f"execution-node:{lease.node_id}"),
        )

    def _consume_result(
        self,
        record: ReplicaReplacementRecord,
        request: ReplicaReplacementRequest,
        result: ReplicaReplacementResult,
    ) -> ReplicaReplacementRecord:
        evidence = record.evidence_refs + result.evidence_refs
        if result.uncertain:
            return replace(
                record,
                state=ReplicaReplacementState.RECONCILE_REQUIRED,
                evidence_refs=evidence,
            )
        if not result.applied:
            self._release_work(request.project_id, record.binding.spec.request.work_id)
            return replace(
                record,
                state=ReplicaReplacementState.FAILED,
                evidence_refs=evidence,
                pending_request=None,
            )
        self._validate_applied_result(request, result)
        self._release_work(request.project_id, record.binding.spec.request.work_id)
        return replace(
            record,
            state=ReplicaReplacementState.SUCCEEDED,
            target_node_id=request.target_node_id,
            evidence_refs=evidence,
            pending_request=None,
        )

    def _reconcile(
        self,
        plan: FleetReplacementPlan,
        record: ReplicaReplacementRecord,
    ) -> ReplicaReplacementRecord:
        request = record.pending_request
        assert request is not None
        self._validate_live_binding(plan, record.binding)
        if request.plan_fingerprint != fleet_replacement_plan_fingerprint(plan):
            raise FleetReplacementError(
                "pending replacement request disagrees with trusted plan fingerprint"
            )
        try:
            result = self.port.inspect(request)
        except Exception:  # noqa: BLE001 - inspection is the only safe recovery boundary.
            raise FleetReplacementError(
                "replacement inspection failed; replay remains blocked"
            ) from None
        if result.uncertain:
            return replace(
                record,
                state=ReplicaReplacementState.RECONCILE_REQUIRED,
                evidence_refs=record.evidence_refs + result.evidence_refs,
            )
        return self._consume_result(record, request, result)

    @staticmethod
    def _validate_applied_result(
        request: ReplicaReplacementRequest,
        result: ReplicaReplacementResult,
    ) -> None:
        if (
            result.observed_node_id != request.target_node_id
            or result.release_sha != request.release_sha
            or result.artifact_digest != request.artifact_digest
            or result.healthy is not True
        ):
            raise FleetReplacementError(
                "replacement success does not prove exact target/release health provenance"
            )

    def _cordon_source(self, node_id: str, prior: bool | None) -> bool:
        node = self._node(node_id)
        source_was_enabled = node.enabled if prior is None else prior
        if node.enabled:
            self.nodes.register(replace(node, enabled=False))
        return source_was_enabled

    def _maybe_resume_source(
        self,
        plan: FleetReplacementPlan,
        record: ReplicaReplacementRecord,
    ) -> None:
        source_node_id = record.binding.spec.source_node_id
        if source_node_id in plan.retain_cordoned_nodes or record.source_was_enabled is not True:
            return
        node = self._node(source_node_id)
        if not node.enabled:
            self.nodes.register(replace(node, enabled=True))

    def _placements(
        self,
        records: dict[tuple[str, str], ReplicaReplacementRecord],
    ) -> dict[tuple[str, str], str]:
        placements = {
            (service.service.service_id, replica.replica_id): replica.node_id
            for service in self.operations.snapshot().services
            for replica in service.service.replicas
        }
        for key, record in records.items():
            if record.state is ReplicaReplacementState.SUCCEEDED:
                assert record.target_node_id is not None
                placements[key] = record.target_node_id
        return placements

    def _cleanup_terminal_leases(
        self,
        records: dict[tuple[str, str], ReplicaReplacementRecord],
    ) -> None:
        for record in records.values():
            if record.state in {
                ReplicaReplacementState.SUCCEEDED,
                ReplicaReplacementState.FAILED,
            }:
                self._release_work(
                    record.binding.spec.request.project_id,
                    record.binding.spec.request.work_id,
                )

    def _validate_pending_leases(
        self,
        plan: FleetReplacementPlan,
        records: dict[tuple[str, str], ReplicaReplacementRecord],
    ) -> None:
        for record in records.values():
            leases = self._matching_leases(
                plan.project_id,
                record.binding.spec.request.work_id,
            )
            if len(leases) > 1:
                raise FleetReplacementError("replacement restore found duplicate work leases")
            if not leases or record.pending_request is None:
                continue
            lease = leases[0]
            if (
                lease.project_id != plan.project_id
                or lease.node_id != record.pending_request.target_node_id
            ):
                raise FleetReplacementError(
                    "replacement restore found mismatched pending work lease"
                )

    def _source_has_other_active_lease(self, node_id: str, work_id: str) -> bool:
        return any(
            lease.node_id == node_id and lease.work_id != work_id
            for lease in self.nodes.snapshot().leases
        )

    def _matching_lease(self, project_id: str, work_id: str) -> WorkLease | None:
        leases = self._matching_leases(project_id, work_id)
        if len(leases) > 1:
            raise FleetReplacementError("replacement work id has multiple active leases")
        return leases[0] if leases else None

    def _matching_leases(
        self,
        project_id: str,
        work_id: str,
    ) -> tuple[WorkLease, ...]:
        return tuple(
            lease
            for lease in self.nodes.snapshot().leases
            if lease.project_id == project_id and lease.work_id == work_id
        )

    def _release_work(self, project_id: str, work_id: str) -> None:
        matches = [
            lease
            for lease in self.nodes.snapshot().leases
            if lease.project_id == project_id and lease.work_id == work_id
        ]
        if len(matches) > 1:
            raise FleetReplacementError("replacement work id has multiple active leases")
        if matches:
            self.nodes.release(matches[0].lease_id)

    def _service(self, service_id: str) -> ServiceRecord:
        matches = [
            record
            for record in self.operations.snapshot().services
            if record.service.service_id == service_id
        ]
        if len(matches) != 1:
            raise FleetReplacementError("replacement requires exactly one operations service")
        return matches[0]

    def _node(self, node_id: str) -> ExecutionNode:
        matches = [
            node
            for node in self.nodes.snapshot().nodes
            if node.identity.node_id == node_id
        ]
        if len(matches) != 1:
            raise FleetReplacementError("replacement requires exactly one execution node")
        return matches[0]

    def _plan(self, plan_id: str) -> FleetReplacementPlan:
        try:
            return self._plans[plan_id]
        except KeyError as exc:
            raise FleetReplacementError("unknown replacement plan") from exc

    @staticmethod
    def _ordered_keys(plan: FleetReplacementPlan) -> tuple[tuple[str, str], ...]:
        return tuple((item.service_id, item.replica_id) for item in plan.replacements)


def fleet_replacement_plan_fingerprint(plan: FleetReplacementPlan) -> str:
    payload = {
        "schema": FLEET_REPLACEMENT_SCHEMA,
        "plan_id": plan.plan_id,
        "project_id": plan.project_id,
        "fleet_plan_id": plan.fleet_plan_id,
        "replacements": [
            {
                "service_id": item.service_id,
                "replica_id": item.replica_id,
                "deployment_operation_id": item.deployment_operation_id,
                "source_node_id": item.source_node_id,
                "request": {
                    "project_id": item.request.project_id,
                    "work_id": item.request.work_id,
                    "platform": item.request.platform.value,
                    "required_features": sorted(item.request.required_features),
                    "required_toolchains": sorted(item.request.required_toolchains),
                    "resources": (
                        item.request.resources.cpu_cores,
                        item.request.resources.memory_mb,
                        item.request.resources.disk_mb,
                    ),
                    "require_gpu": item.request.require_gpu,
                },
            }
            for item in plan.replacements
        ],
        "budgets": [
            (
                item.environment_id,
                item.max_unavailable_replicas,
                item.max_concurrent_replacements,
            )
            for item in sorted(plan.budgets, key=lambda value: value.environment_id)
        ],
        "max_replicas_per_node": plan.max_replicas_per_node,
        "authorization_work_id": plan.authorization_work_id,
        "approval_ref": plan.approval_ref,
        "reason": plan.reason,
        "evidence_refs": plan.evidence_refs,
        "retain_cordoned_nodes": sorted(plan.retain_cordoned_nodes),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _authority_binding_ref(plan: FleetReplacementPlan) -> str:
    return f"fleet-replacement-plan:{fleet_replacement_plan_fingerprint(plan)}"


def _fleet_state(records: tuple[ReplicaReplacementRecord, ...]) -> FleetReplacementState:
    states = {record.state for record in records}
    if states == {ReplicaReplacementState.SUCCEEDED}:
        return FleetReplacementState.SUCCEEDED
    if states <= {ReplicaReplacementState.SUCCEEDED, ReplicaReplacementState.FAILED}:
        return FleetReplacementState.PARTIAL_FAILURE
    if states & _ACTIVE_STATES:
        return FleetReplacementState.RECONCILE_REQUIRED
    if states & _BLOCKED_STATES:
        return FleetReplacementState.BLOCKED
    if ReplicaReplacementState.FAILED in states:
        return FleetReplacementState.IN_PROGRESS
    if states == {ReplicaReplacementState.PENDING}:
        return FleetReplacementState.PENDING
    return FleetReplacementState.IN_PROGRESS


def _validate_sha(value: str) -> None:
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise FleetReplacementError("replacement release SHA is invalid")


def _validate_digest(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise FleetReplacementError("replacement digest is invalid")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise FleetReplacementError("replacement datetime must be timezone-aware")
    return value.astimezone(UTC)
