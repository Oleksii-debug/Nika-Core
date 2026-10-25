from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from nika_core.product_factory_orchestration import (
    OwnershipLease,
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryGraphError,
    RepositoryRef,
)
from nika_core.product_factory_review_authority import (
    ProductFactoryReviewAuthorityPort,
    ProductFactoryReviewSubject,
)
from nika_core.toolsmith.contracts import (
    ArtifactEvidence,
    ChangedFile,
    CodingResult,
    RecoveryState,
    TestEvidence,
    WorkerFailure,
    WorkerFailureKind,
)

_MAX_DURABLE_TEXT_UTF8_BYTES = 4096
_MAX_EVIDENCE_REF_UTF8_BYTES = 4096
_MAX_REVIEW_EVIDENCE_REFS = 32
_MAX_REVIEW_EVIDENCE_UTF8_BYTES = 16384
_MAX_REPAIR_REASON_UTF8_BYTES = _MAX_DURABLE_TEXT_UTF8_BYTES
_MAX_CANCELLATION_REASON_UTF8_BYTES = _MAX_DURABLE_TEXT_UTF8_BYTES
_UNICODE_LINE_BOUNDARY_CODEPOINTS = frozenset({0x85, 0x2028, 0x2029})


class CoordinatorError(ValueError):
    """Raised when Product Factory orchestration invariants are violated."""


class WorkState(StrEnum):
    PLANNED = "planned"
    READY = "ready"
    RUNNING = "running"
    REVIEW_REQUIRED = "review_required"
    ACCEPTED = "accepted"
    REPAIR_REQUIRED = "repair_required"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"


def _validate_allowed_paths(value: object) -> None:
    if (
        type(value) is not tuple
        or not value
        or any(type(path) is not str or not path for path in value)
    ):
        raise CoordinatorError("work request allowed paths must be a non-empty exact tuple of strings")


def _validate_permission_ceiling(value: object) -> None:
    if (
        type(value) is not frozenset
        or not value
        or any(type(permission) is not str or not permission for permission in value)
    ):
        raise CoordinatorError(
            "work request permission ceiling must be a non-empty exact frozenset of strings"
        )


def _validate_acceptance_commands(value: object) -> None:
    if type(value) is not tuple or any(
        type(command) is not tuple
        or not command
        or any(type(part) is not str or not part for part in command)
        for command in value
    ):
        raise CoordinatorError(
            "work request acceptance commands must be an exact tuple of non-empty argv tuples"
        )


@dataclass(frozen=True, slots=True)
class ComponentWorkRequest:
    work_id: str
    project_id: str
    component_id: str
    repository_id: str
    goal: str
    base_sha: str
    allowed_paths: tuple[str, ...]
    permission_ceiling: frozenset[str]
    acceptance_commands: tuple[tuple[str, ...], ...]
    attempt: int = 1

    def __post_init__(self) -> None:
        _validate_work_request_scalar_authority(self)
        _validate_allowed_paths(self.allowed_paths)
        _validate_permission_ceiling(self.permission_ceiling)
        _validate_acceptance_commands(self.acceptance_commands)


@dataclass(frozen=True, slots=True)
class WorkerResultEnvelope:
    work_id: str
    component_id: str
    repository_id: str
    base_sha: str
    result_sha: str
    diff_digest: str
    coding_result: CodingResult
    producer_actor_id: str | None = None

    def __post_init__(self) -> None:
        _validate_worker_result_scalar_authority(self)


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    reviewer_id: str
    accepted: bool
    reason: str
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_review_decision(self)


@dataclass(frozen=True, slots=True)
class WorkRecord:
    request: ComponentWorkRequest
    state: WorkState
    result: WorkerResultEnvelope | None = None
    review: ReviewDecision | None = None
    blocker: str | None = None

    def __post_init__(self) -> None:
        if type(self.state) is not WorkState:
            raise CoordinatorError("work state must be an exact WorkState")


@dataclass(frozen=True, slots=True)
class CoordinatorSnapshot:
    project_id: str
    revision: int
    records: tuple[WorkRecord, ...]
    trusted_plan: tuple[ComponentWorkRequest, ...] | None = None


class ComponentDispatcherPort(Protocol):
    async def dispatch(self, request: ComponentWorkRequest) -> WorkerResultEnvelope: ...


@dataclass(slots=True)
class ProductFactoryCoordinator:
    """PF4 coordinator above bounded component work, not a second agent runtime."""

    graph: ProductRepositoryGraph
    review_authority: ProductFactoryReviewAuthorityPort | None = field(
        default=None,
        repr=False,
    )
    _records: dict[str, WorkRecord] = field(default_factory=dict, init=False, repr=False)
    _revision: int = field(default=0, init=False, repr=False)
    _trusted_plan: tuple[ComponentWorkRequest, ...] | None = field(default=None, init=False, repr=False)
    _trusted_plan_fingerprint: str | None = field(default=None, init=False, repr=False)

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def trusted_plan_fingerprint(self) -> str:
        if self._trusted_plan_fingerprint is None:
            raise CoordinatorError("coordinator has no established trusted plan authority")
        return self._trusted_plan_fingerprint

    def plan(self, *, base_shas: dict[str, str], goals: dict[str, str], permission_ceiling: frozenset[str]) -> CoordinatorSnapshot:
        if self._records or self._trusted_plan is not None:
            raise CoordinatorError("coordinator is already planned")
        _validate_permission_ceiling(permission_ceiling)
        components = self._components()
        repositories = self._repositories()
        for component_id in self.graph.dependency_order():
            component = components[component_id]
            _validate_allowed_paths(component.paths)
            _validate_acceptance_commands(component.test_commands)
            repository = repositories[component.repository_id]
            base_sha = base_shas.get(repository.repository_id)
            raw_goal = goals.get(component_id)
            if base_sha is None or raw_goal is None:
                raise CoordinatorError(f"missing base SHA or goal for component {component_id}")
            _validate_sha(base_sha, "base_sha")
            if type(raw_goal) is not str:
                raise CoordinatorError("component goal must be an exact string")
            goal = raw_goal.strip()
            if not goal:
                raise CoordinatorError(f"missing base SHA or goal for component {component_id}")
            request = ComponentWorkRequest(
                work_id=_work_id(project_id=self.graph.project_id, component_id=component_id, repository_id=repository.repository_id, goal=goal, base_sha=base_sha, allowed_paths=component.paths, permission_ceiling=permission_ceiling, acceptance_commands=component.test_commands, attempt=1),
                project_id=self.graph.project_id, component_id=component_id, repository_id=repository.repository_id,
                goal=goal, base_sha=base_sha, allowed_paths=component.paths, permission_ceiling=permission_ceiling,
                acceptance_commands=component.test_commands,
            )
            self._records[component_id] = WorkRecord(request=request, state=WorkState.PLANNED)
        self._trusted_plan = tuple(self._records[c].request for c in sorted(self._records))
        self._trusted_plan_fingerprint = trusted_plan_fingerprint(self._trusted_plan)
        self._advance_ready()
        return self.snapshot()

    def ready_requests(self) -> tuple[ComponentWorkRequest, ...]:
        return tuple(record.request for _, record in sorted(self._records.items()) if record.state is WorkState.READY)

    def start(self, component_id: str) -> ComponentWorkRequest:
        record = self._record(component_id)
        if record.state in {WorkState.DONE, WorkState.CANCELLED}:
            raise CoordinatorError(
                f"component {component_id} is terminal ({record.state.value}) and cannot be started"
            )
        if record.state is not WorkState.READY:
            raise CoordinatorError(f"component {component_id} is not ready")
        self._records[component_id] = WorkRecord(record.request, WorkState.RUNNING)
        self._touch()
        return record.request

    def record_result(self, envelope: WorkerResultEnvelope) -> WorkRecord:
        envelope = _canonical_worker_result_envelope(envelope)
        record = self._record(envelope.component_id)
        if record.state is not WorkState.RUNNING:
            raise CoordinatorError("worker result is only valid for a running component")
        request = record.request
        self._validate_result_identity(request, envelope)
        if not envelope.coding_result.succeeded:
            blocker = _canonical_worker_failure_message_from_result(
                envelope.coding_result,
                missing_error="failed worker result requires failure evidence",
            )
            updated = WorkRecord(request, WorkState.REPAIR_REQUIRED, envelope, blocker=blocker)
        else:
            self._validate_success_evidence(request, envelope.coding_result.test_evidence)
            updated = WorkRecord(request, WorkState.REVIEW_REQUIRED, envelope)
        self._records[envelope.component_id] = updated
        self._touch()
        return updated

    def review(self, component_id: str, decision: ReviewDecision) -> WorkRecord:
        decision = _canonical_review_decision(decision)
        record = self._record(component_id)
        if record.state is not WorkState.REVIEW_REQUIRED or record.result is None:
            raise CoordinatorError("component is not awaiting independent review")
        self._verify_trusted_review(record, decision)
        state = WorkState.ACCEPTED if decision.accepted else WorkState.REPAIR_REQUIRED
        updated = WorkRecord(record.request, state, record.result, review=decision, blocker=None if decision.accepted else decision.reason)
        self._records[component_id] = updated
        self._touch()
        self._advance_ready()
        return updated

    def mark_done(self, component_id: str) -> WorkRecord:
        record = self._record(component_id)
        if record.state is WorkState.DONE:
            return record
        if record.state is not WorkState.ACCEPTED:
            raise CoordinatorError(f"component {component_id} cannot be marked done from {record.state.value}")
        updated = WorkRecord(record.request, WorkState.DONE, record.result, record.review)
        self._records[component_id] = updated
        self._touch()
        self._advance_ready()
        return updated

    def cancel(self, component_id: str, *, reason: str) -> WorkRecord:
        reason = _canonical_cancellation_reason(reason)
        record = self._record(component_id)
        if record.state is WorkState.RUNNING:
            raise CoordinatorError("running component requires execution stop and fence proof")
        if record.state in {WorkState.BLOCKED, WorkState.ACCEPTED, WorkState.DONE}:
            raise CoordinatorError(f"{record.state.value} component cannot be cancelled")
        if record.state is WorkState.CANCELLED:
            if record.blocker != reason:
                raise CoordinatorError("cancelled component reason cannot be rebound")
            return record
        updated = WorkRecord(
            record.request,
            WorkState.CANCELLED,
            record.result,
            record.review,
            reason,
        )
        self._records[component_id] = updated
        self._touch()
        return updated

    def prepare_repair(self, component_id: str, *, base_sha: str, reason: str) -> ComponentWorkRequest:
        _validate_sha(base_sha, "base_sha")
        record = self._record(component_id)
        if record.state is not WorkState.REPAIR_REQUIRED:
            raise CoordinatorError("repair can only be prepared from repair_required")
        reason = _canonical_repair_reason(reason)
        attempt = record.request.attempt + 1
        goal = f"{record.request.goal}\nRepair: {reason}"
        request = ComponentWorkRequest(
            work_id=_work_id(project_id=self.graph.project_id, component_id=component_id, repository_id=record.request.repository_id, goal=goal, base_sha=base_sha, allowed_paths=record.request.allowed_paths, permission_ceiling=record.request.permission_ceiling, acceptance_commands=record.request.acceptance_commands, attempt=attempt),
            project_id=record.request.project_id, component_id=record.request.component_id, repository_id=record.request.repository_id,
            goal=goal, base_sha=base_sha, allowed_paths=record.request.allowed_paths,
            permission_ceiling=record.request.permission_ceiling, acceptance_commands=record.request.acceptance_commands, attempt=attempt,
        )
        self._records[component_id] = WorkRecord(request, WorkState.READY)
        self._touch()
        return request

    def block(self, component_id: str, reason: str) -> WorkRecord:
        reason = _canonical_durable_text(reason, label="blocker reason")
        record = self._record(component_id)
        if record.state is WorkState.RUNNING:
            raise CoordinatorError("running component requires execution stop and fence proof")
        if record.state is WorkState.BLOCKED:
            if record.blocker != reason:
                raise CoordinatorError("blocked component reason cannot be rebound")
            return record
        if record.state in {WorkState.ACCEPTED, WorkState.DONE, WorkState.CANCELLED}:
            raise CoordinatorError(f"{record.state.value} component cannot be blocked")
        if record.result is not None or record.review is not None:
            raise CoordinatorError("component with result or review evidence cannot be blocked")
        updated = WorkRecord(record.request, WorkState.BLOCKED, blocker=reason)
        self._records[component_id] = updated
        self._touch()
        return updated

    def snapshot(self) -> CoordinatorSnapshot:
        return CoordinatorSnapshot(self.graph.project_id, self._revision, tuple(self._records[key] for key in sorted(self._records)), self._trusted_plan)

    def restore(self, snapshot: CoordinatorSnapshot, *, trusted_plan_fingerprint: str | None = None) -> None:
        _validate_coordinator_snapshot_carrier(snapshot)
        if trusted_plan_fingerprint is not None:
            _validate_digest(trusted_plan_fingerprint, "trusted_plan_fingerprint")
            authority = trusted_plan_fingerprint
        else:
            authority = self._trusted_plan_fingerprint
            if authority is None:
                raise CoordinatorError("fresh coordinator restore requires external trusted plan authority")
            _validate_digest(authority, "trusted_plan_fingerprint")
        validate_trusted_plan_snapshot(snapshot, authority)
        if snapshot.project_id != self.graph.project_id:
            raise CoordinatorError("snapshot project does not match repository graph")
        if snapshot.revision < 0:
            raise CoordinatorError("snapshot revision must be non-negative")
        components = self._components()
        repositories = self._repositories()
        expected = set(components)
        actual = [record.request.component_id for record in snapshot.records]
        if set(actual) != expected or len(actual) != len(set(actual)):
            raise CoordinatorError("snapshot component set does not match repository graph")
        plan = snapshot.trusted_plan
        if plan is None:
            raise CoordinatorError("snapshot is missing immutable trusted plan descriptor")
        plan_by_component = {request.component_id: request for request in plan}
        if set(plan_by_component) != expected:
            raise CoordinatorError("trusted plan component set does not match repository graph")
        for component_id, initial in plan_by_component.items():
            component = components[component_id]
            repository = repositories[component.repository_id]
            if initial.project_id != self.graph.project_id:
                raise CoordinatorError("trusted plan project identity drifted")
            if initial.repository_id != repository.repository_id:
                raise CoordinatorError("trusted plan repository identity drifted")
            if initial.allowed_paths != component.paths:
                raise CoordinatorError("trusted plan path scope drifted")
            if initial.acceptance_commands != component.test_commands:
                raise CoordinatorError("trusted plan acceptance command scope drifted")
        if snapshot.records:
            permission_ceilings = {record.request.permission_ceiling for record in snapshot.records}
            if len(permission_ceilings) != 1:
                raise CoordinatorError("snapshot work requests disagree on project permission ceiling")
        canonical_records: list[WorkRecord] = []
        for record in snapshot.records:
            canonical_record = _canonical_restored_record(record)
            request = canonical_record.request
            component = components[request.component_id]
            repository = repositories[component.repository_id]
            if request.project_id != self.graph.project_id:
                raise CoordinatorError("snapshot work request project identity drifted")
            if request.repository_id != repository.repository_id:
                raise CoordinatorError("snapshot work request repository identity drifted")
            if request.allowed_paths != component.paths:
                raise CoordinatorError("snapshot work request path scope drifted")
            if request.acceptance_commands != component.test_commands:
                raise CoordinatorError("snapshot acceptance command scope drifted")
            self._validate_restored_record(canonical_record)
            canonical_records.append(canonical_record)
        restored_records = tuple(canonical_records)
        self._validate_restored_dependencies(restored_records)
        self._records = {
            record.request.component_id: record for record in restored_records
        }
        self._revision = snapshot.revision
        self._trusted_plan = plan
        self._trusted_plan_fingerprint = authority
        self._advance_ready()

    def _validate_restored_record(self, record: WorkRecord) -> None:
        if type(record.state) is not WorkState:
            raise CoordinatorError("snapshot work state must be an exact WorkState")
        request, result, review, blocker = record.request, record.result, record.review, record.blocker
        if result is not None:
            self._validate_result_identity(request, result)
        if review is not None:
            _validate_review_decision(review)
        if record.state in {WorkState.PLANNED, WorkState.READY, WorkState.RUNNING}:
            if result is not None or review is not None or blocker is not None:
                raise CoordinatorError("pre-result snapshot work contains terminal evidence")
            return
        if record.state is WorkState.REVIEW_REQUIRED:
            if result is None or not result.coding_result.succeeded or review is not None or blocker is not None:
                raise CoordinatorError("review_required snapshot work requires one successful result only")
            self._validate_success_evidence(request, result.coding_result.test_evidence)
            return
        if record.state in {WorkState.ACCEPTED, WorkState.DONE}:
            if result is None or not result.coding_result.succeeded or review is None or not review.accepted or blocker is not None:
                raise CoordinatorError(f"{record.state.value} snapshot work requires successful result and accepted review")
            self._validate_success_evidence(request, result.coding_result.test_evidence)
            self._verify_trusted_review(record, review)
            return
        if record.state is WorkState.CANCELLED:
            if blocker is None:
                raise CoordinatorError("cancelled snapshot work requires cancellation reason")
            _canonical_cancellation_reason(blocker)
            if review is not None:
                if result is None or not result.coding_result.succeeded or review.accepted:
                    raise CoordinatorError(
                        "cancelled snapshot review evidence is internally inconsistent"
                    )
                self._validate_success_evidence(request, result.coding_result.test_evidence)
                self._verify_trusted_review(record, review)
                return
            if result is not None:
                if result.coding_result.succeeded:
                    self._validate_success_evidence(request, result.coding_result.test_evidence)
                else:
                    _canonical_worker_failure_message_from_result(
                        result.coding_result,
                        missing_error="cancelled worker-failed snapshot requires failure evidence",
                    )
            return
        if record.state is WorkState.REPAIR_REQUIRED:
            if result is None or blocker is None:
                raise CoordinatorError("repair_required snapshot work requires result evidence and blocker")
            if result.coding_result.succeeded:
                _canonical_durable_text(blocker, label="repair blocker")
                if review is None or review.accepted or blocker != review.reason:
                    raise CoordinatorError("review-rejected repair snapshot is internally inconsistent")
                self._validate_success_evidence(request, result.coding_result.test_evidence)
                self._verify_trusted_review(record, review)
            else:
                if review is not None:
                    raise CoordinatorError("worker-failed repair snapshot cannot contain review evidence")
                failure_message = _canonical_worker_failure_message_from_result(
                    result.coding_result,
                    missing_error="worker-failed repair snapshot requires failure evidence",
                )
                canonical_blocker = _canonical_worker_failure_message(blocker)
                if canonical_blocker != failure_message:
                    raise CoordinatorError(
                        "worker-failed repair blocker does not match failure evidence"
                    )
            return
        if record.state is WorkState.BLOCKED:
            if result is not None or review is not None or blocker is None:
                raise CoordinatorError("blocked snapshot work requires blocker without terminal evidence")
            _canonical_durable_text(blocker, label="blocker reason")
            return
        raise CoordinatorError("snapshot contains unknown work state")


    def _verify_trusted_review(
        self,
        record: WorkRecord,
        decision: ReviewDecision,
    ) -> None:
        result = record.result
        if result is None:
            raise CoordinatorError(
                "trusted independent review requires worker result evidence"
            )
        if result.producer_actor_id is None:
            raise CoordinatorError(
                "trusted independent review requires producer actor identity"
            )
        if result.producer_actor_id == decision.reviewer_id:
            raise CoordinatorError(
                "independent reviewer must differ from candidate producer"
            )
        if self.review_authority is None:
            raise CoordinatorError("trusted independent review authority is required")
        subject = ProductFactoryReviewSubject(
            project_id=record.request.project_id,
            component_id=record.request.component_id,
            work_id=record.request.work_id,
            repository_id=record.request.repository_id,
            base_sha=record.request.base_sha,
            result_sha=result.result_sha,
            diff_digest=result.diff_digest,
            attempt=record.request.attempt,
            producer_actor_id=result.producer_actor_id,
            reviewer_id=decision.reviewer_id,
            accepted=decision.accepted,
        )
        try:
            verified = self.review_authority.verify(subject, decision.evidence_refs)
        except Exception as exc:
            raise CoordinatorError(
                "trusted independent review authority verification failed"
            ) from exc
        if verified is not True:
            raise CoordinatorError(
                "trusted independent review authority rejected decision"
            )

    def _validate_restored_dependencies(self, records: tuple[WorkRecord, ...]) -> None:
        satisfied = {record.request.component_id for record in records if record.state in {WorkState.ACCEPTED, WorkState.DONE}}
        components = self._components()
        states_requiring_dependencies = {WorkState.READY, WorkState.RUNNING, WorkState.REVIEW_REQUIRED, WorkState.ACCEPTED, WorkState.DONE, WorkState.REPAIR_REQUIRED}
        for record in records:
            if record.state not in states_requiring_dependencies:
                continue
            dependencies = set(components[record.request.component_id].dependencies)
            if not dependencies <= satisfied:
                raise CoordinatorError("snapshot component state bypasses dependency acceptance")

    def _validate_result_identity(
        self,
        request: ComponentWorkRequest,
        envelope: WorkerResultEnvelope,
    ) -> None:
        _validate_work_request_scalar_authority(request)
        _validate_worker_result_scalar_authority(envelope)
        if envelope.component_id != request.component_id:
            raise CoordinatorError("worker result component does not match active request")
        if envelope.work_id != request.work_id or envelope.repository_id != request.repository_id:
            raise CoordinatorError("worker result identity does not match active request")
        if envelope.base_sha != request.base_sha:
            raise CoordinatorError("stale worker result base SHA does not match active request")
        if envelope.coding_result.job_id != request.work_id:
            raise CoordinatorError("coding result job id does not match Product Factory work id")
        changed_files = envelope.coding_result.changed_files
        if not changed_files:
            return
        try:
            self.graph.assess_lease(
                OwnershipLease(
                    lease_id=f"result-scope:{request.work_id}",
                    worker_id="product-factory-result",
                    component_ids=(request.component_id,),
                    allowed_paths=tuple(item.path for item in changed_files),
                ),
                (),
            )
        except RepositoryGraphError as exc:
            raise CoordinatorError(
                "worker result changed files exceed active request path scope"
            ) from exc

    @staticmethod
    def _validate_success_evidence(
        request: ComponentWorkRequest,
        evidence: tuple[TestEvidence, ...],
    ) -> None:
        _validate_test_evidence_carrier(evidence)
        if not evidence or any(item.exit_code != 0 for item in evidence):
            raise CoordinatorError("successful worker result requires passing test evidence")
        remaining = list(evidence)
        for declared in request.acceptance_commands:
            match_index = next((index for index, item in enumerate(remaining) if _commands_equivalent(item.command, declared, component_id=request.component_id)), None)
            if match_index is None:
                raise CoordinatorError("successful worker result must prove every declared acceptance command")
            remaining.pop(match_index)

    def _advance_ready(self) -> None:
        satisfied = {key for key, item in self._records.items() if item.state in {WorkState.ACCEPTED, WorkState.DONE}}
        components = self._components()
        changed = False
        for component_id, record in tuple(self._records.items()):
            if record.state is WorkState.PLANNED and set(components[component_id].dependencies) <= satisfied:
                self._records[component_id] = WorkRecord(record.request, WorkState.READY)
                changed = True
        if changed:
            self._touch()

    def _components(self) -> dict[str, ProductComponent]:
        return {component.component_id: component for component in self.graph.components}

    def _repositories(self) -> dict[str, RepositoryRef]:
        return {repository.repository_id: repository for repository in self.graph.repositories}

    def _record(self, component_id: str) -> WorkRecord:
        try:
            return self._records[component_id]
        except KeyError as exc:
            raise CoordinatorError(f"unknown component {component_id}") from exc

    def _touch(self) -> None:
        self._revision += 1



def _canonical_review_decision(value: object) -> ReviewDecision:
    if type(value) is not ReviewDecision:
        raise CoordinatorError(
            "independent review decision must be exact ReviewDecision"
        )
    _validate_review_decision(value)
    return ReviewDecision(
        reviewer_id=value.reviewer_id,
        accepted=value.accepted,
        reason=value.reason,
        evidence_refs=value.evidence_refs,
    )


def _canonical_worker_result_envelope(value: object) -> WorkerResultEnvelope:
    if type(value) is not WorkerResultEnvelope:
        raise CoordinatorError("worker result must be exact WorkerResultEnvelope")
    _validate_worker_result_scalar_authority(value)
    source = value.coding_result
    canonical_result = CodingResult(
        job_id=source.job_id,
        changed_files=tuple(
            _reconstruct_changed_file(item) for item in source.changed_files
        ),
        test_evidence=tuple(
            _reconstruct_test_evidence(item) for item in source.test_evidence
        ),
        artifacts=tuple(
            _reconstruct_artifact_evidence(item) for item in source.artifacts
        ),
        recovery_state=(
            None
            if source.recovery_state is None
            else _reconstruct_recovery_state(source.recovery_state)
        ),
        failure=(
            None
            if source.failure is None
            else _reconstruct_worker_failure(source.failure)
        ),
    )
    canonical = WorkerResultEnvelope(
        work_id=value.work_id,
        component_id=value.component_id,
        repository_id=value.repository_id,
        base_sha=value.base_sha,
        result_sha=value.result_sha,
        diff_digest=value.diff_digest,
        coding_result=canonical_result,
        producer_actor_id=value.producer_actor_id,
    )
    _validate_worker_result_scalar_authority(canonical)
    return canonical


def _reconstruct_changed_file(value: ChangedFile) -> ChangedFile:
    try:
        return ChangedFile(value.path, value.sha256, value.size_bytes)
    except (TypeError, ValueError) as exc:
        raise CoordinatorError(
            "worker result contains invalid changed-file evidence"
        ) from exc


def _reconstruct_test_evidence(value: TestEvidence) -> TestEvidence:
    try:
        return TestEvidence(value.command, value.exit_code, value.output_digest)
    except (TypeError, ValueError) as exc:
        raise CoordinatorError("worker result contains invalid test evidence") from exc


def _reconstruct_artifact_evidence(value: ArtifactEvidence) -> ArtifactEvidence:
    try:
        return ArtifactEvidence(value.name, value.digest, value.media_type)
    except (TypeError, ValueError) as exc:
        raise CoordinatorError(
            "worker result contains invalid artifact evidence"
        ) from exc


def _reconstruct_recovery_state(value: RecoveryState) -> RecoveryState:
    try:
        return RecoveryState(value.phase, value.opaque_token)
    except (TypeError, ValueError) as exc:
        raise CoordinatorError(
            "worker result contains invalid recovery-state evidence"
        ) from exc


def _reconstruct_worker_failure(value: WorkerFailure) -> WorkerFailure:
    try:
        return WorkerFailure(value.kind, value.message, value.retryable)
    except (TypeError, ValueError) as exc:
        raise CoordinatorError("worker result contains invalid failure evidence") from exc


def _canonical_restored_record(record: WorkRecord) -> WorkRecord:
    if type(record) is not WorkRecord:
        raise CoordinatorError("snapshot records must be exact WorkRecord values")
    result = (
        None
        if record.result is None
        else _canonical_worker_result_envelope(record.result)
    )
    review = (
        None
        if record.review is None
        else _canonical_review_decision(record.review)
    )
    return WorkRecord(
        request=record.request,
        state=record.state,
        result=result,
        review=review,
        blocker=record.blocker,
    )


def _validate_coordinator_snapshot_carrier(snapshot: object) -> None:
    if type(snapshot) is not CoordinatorSnapshot:
        raise CoordinatorError("snapshot must be an exact CoordinatorSnapshot")
    if type(snapshot.project_id) is not str or not snapshot.project_id.strip():
        raise CoordinatorError("snapshot project id must be an exact non-empty string")
    if type(snapshot.revision) is not int or snapshot.revision < 0:
        raise CoordinatorError("snapshot revision must be an exact non-negative integer")
    if type(snapshot.records) is not tuple or any(
        type(record) is not WorkRecord for record in snapshot.records
    ):
        raise CoordinatorError("snapshot records must be an exact tuple of WorkRecord")
    if snapshot.trusted_plan is not None and (
        type(snapshot.trusted_plan) is not tuple
        or any(type(request) is not ComponentWorkRequest for request in snapshot.trusted_plan)
    ):
        raise CoordinatorError(
            "snapshot trusted plan must be an exact tuple of ComponentWorkRequest"
        )


def trusted_plan_fingerprint(plan: tuple[ComponentWorkRequest, ...]) -> str:
    if type(plan) is not tuple:
        raise CoordinatorError("trusted plan descriptor must be an exact tuple")
    if not plan:
        raise CoordinatorError("trusted plan descriptor must not be empty")
    for request in plan:
        _validate_work_request_scalar_authority(request)
        _validate_allowed_paths(request.allowed_paths)
        _validate_permission_ceiling(request.permission_ceiling)
        _validate_acceptance_commands(request.acceptance_commands)
    payload = tuple((request.project_id, request.component_id, request.repository_id, request.goal, request.base_sha, request.allowed_paths, tuple(sorted(request.permission_ceiling)), request.acceptance_commands) for request in sorted(plan, key=lambda item: item.component_id))
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_trusted_plan_snapshot(snapshot: CoordinatorSnapshot, authority_fingerprint: str) -> None:
    _validate_coordinator_snapshot_carrier(snapshot)
    _validate_digest(authority_fingerprint, "trusted_plan_fingerprint")
    plan = snapshot.trusted_plan
    if plan is None or not plan:
        raise CoordinatorError("snapshot is missing immutable trusted plan descriptor")
    if trusted_plan_fingerprint(plan) != authority_fingerprint:
        raise CoordinatorError("snapshot trusted plan does not match external authority")
    for request in plan:
        _validate_work_request_scalar_authority(request)
    for record in snapshot.records:
        _validate_work_request_scalar_authority(record.request)
    plan_ids = [request.component_id for request in plan]
    if len(plan_ids) != len(set(plan_ids)):
        raise CoordinatorError("trusted plan repeats component identity")
    plan_by_component = {request.component_id: request for request in plan}
    record_ids = [record.request.component_id for record in snapshot.records]
    if set(record_ids) != set(plan_by_component) or len(record_ids) != len(set(record_ids)):
        raise CoordinatorError("snapshot work set does not match trusted plan descriptor")
    for initial in plan:
        if initial.project_id != snapshot.project_id:
            raise CoordinatorError("trusted plan project identity does not match snapshot")
        if initial.attempt != 1:
            raise CoordinatorError("trusted plan descriptor must contain attempt-one requests")
        expected_initial_work_id = _work_id(project_id=initial.project_id, component_id=initial.component_id, repository_id=initial.repository_id, goal=initial.goal, base_sha=initial.base_sha, allowed_paths=initial.allowed_paths, permission_ceiling=initial.permission_ceiling, acceptance_commands=initial.acceptance_commands, attempt=1)
        if initial.work_id != expected_initial_work_id:
            raise CoordinatorError("trusted plan contains invalid attempt-one work identity")
    for record in snapshot.records:
        request = record.request
        _validate_work_request_scalar_authority(request)
        _validate_allowed_paths(request.allowed_paths)
        _validate_permission_ceiling(request.permission_ceiling)
        _validate_acceptance_commands(request.acceptance_commands)
        initial = plan_by_component[request.component_id]
        if request.project_id != initial.project_id:
            raise CoordinatorError("work request project identity drifted from trusted plan")
        if request.repository_id != initial.repository_id:
            raise CoordinatorError("work request repository identity drifted from trusted plan")
        if request.allowed_paths != initial.allowed_paths:
            raise CoordinatorError("work request path scope drifted from trusted plan")
        if request.permission_ceiling != initial.permission_ceiling:
            raise CoordinatorError("work request permission ceiling drifted from trusted plan")
        if request.acceptance_commands != initial.acceptance_commands:
            raise CoordinatorError("work request acceptance commands drifted from trusted plan")
        if request.attempt == 1:
            if request.goal != initial.goal:
                raise CoordinatorError("attempt-one work goal drifted from trusted plan")
            if request.base_sha != initial.base_sha:
                raise CoordinatorError("attempt-one base SHA drifted from trusted plan")
        elif not _valid_repair_goal(initial.goal, request.goal, request.attempt):
            raise CoordinatorError("repair work goal is not derived from trusted attempt-one plan")
        expected_work_id = _work_id(project_id=request.project_id, component_id=request.component_id, repository_id=request.repository_id, goal=request.goal, base_sha=request.base_sha, allowed_paths=request.allowed_paths, permission_ceiling=request.permission_ceiling, acceptance_commands=request.acceptance_commands, attempt=request.attempt)
        if request.work_id != expected_work_id:
            raise CoordinatorError("snapshot work id does not match durable request identity")


def _valid_repair_goal(initial_goal: str, current_goal: str, attempt: int) -> bool:
    if attempt <= 1 or not current_goal.startswith(initial_goal):
        return False
    suffix = current_goal[len(initial_goal):]
    marker = "\nRepair: "
    if not suffix.startswith(marker):
        return False
    reasons = suffix.split(marker)[1:]
    if len(reasons) != attempt - 1:
        return False
    try:
        return all(_canonical_repair_reason(reason) == reason for reason in reasons)
    except CoordinatorError:
        return False


def _validate_review_decision(decision: ReviewDecision) -> None:
    if type(decision) is not ReviewDecision:
        raise CoordinatorError("review decision must be an exact ReviewDecision")
    if type(decision.accepted) is not bool:
        raise CoordinatorError("review acceptance must be an exact boolean")
    _canonical_durable_text(decision.reviewer_id, label="reviewer id")
    _canonical_durable_text(decision.reason, label="review reason")
    evidence_refs = decision.evidence_refs
    if type(evidence_refs) is not tuple:
        raise CoordinatorError("independent review evidence refs must be canonical text")
    if not evidence_refs:
        raise CoordinatorError("independent review requires reviewer, reason and evidence")
    if (
        len(evidence_refs) > _MAX_REVIEW_EVIDENCE_REFS
        or any(not _canonical_evidence_ref(reference) for reference in evidence_refs)
        or sum(len(reference.encode("utf-8")) for reference in evidence_refs)
        > _MAX_REVIEW_EVIDENCE_UTF8_BYTES
    ):
        raise CoordinatorError("independent review evidence refs must be canonical text")


def _canonical_durable_text(
    value: object,
    *,
    label: str,
    max_utf8_bytes: int = _MAX_DURABLE_TEXT_UTF8_BYTES,
) -> str:
    error = f"{label} must be canonical single-line text"
    if type(value) is not str or not value or value != value.strip():
        raise CoordinatorError(error)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CoordinatorError(error) from exc
    if (
        len(encoded) > max_utf8_bytes
        or any(
            ord(character) < 32
            or ord(character) == 127
            or ord(character) in _UNICODE_LINE_BOUNDARY_CODEPOINTS
            for character in value
        )
    ):
        raise CoordinatorError(error)
    return value


def _canonical_worker_failure_message(value: object) -> str:
    return _canonical_durable_text(value, label="worker failure message")


def _canonical_worker_failure_message_from_result(
    result: CodingResult,
    *,
    missing_error: str,
) -> str:
    failure = result.failure
    if failure is None:
        raise CoordinatorError(missing_error)
    _validate_worker_failure_carrier(failure)
    return _canonical_worker_failure_message(failure.message)


def _canonical_evidence_ref(value: object) -> bool:
    if type(value) is not str or not value or value != value.strip():
        return False
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return (
        len(encoded) <= _MAX_EVIDENCE_REF_UTF8_BYTES
        and not any(
            ord(character) < 32
            or ord(character) == 127
            or ord(character) in _UNICODE_LINE_BOUNDARY_CODEPOINTS
            for character in value
        )
    )


def _canonical_repair_reason(value: object) -> str:
    value = _canonical_durable_text(
        value,
        label="repair reason",
        max_utf8_bytes=_MAX_REPAIR_REASON_UTF8_BYTES,
    )
    if "\nRepair: " in value:
        raise CoordinatorError("repair reason must be canonical single-line text")
    return value


def _canonical_cancellation_reason(value: object) -> str:
    return _canonical_durable_text(
        value,
        label="cancellation reason",
        max_utf8_bytes=_MAX_CANCELLATION_REASON_UTF8_BYTES,
    )


def _commands_equivalent(observed: tuple[str, ...], declared: tuple[str, ...], *, component_id: str) -> bool:
    if observed == declared:
        return True
    observed_pytest = _pytest_args(observed)
    declared_pytest = _pytest_args(declared)
    if observed_pytest is None or declared_pytest is None:
        return False
    if not observed_pytest:
        return not declared_pytest
    if observed_pytest == declared_pytest:
        return True
    if len(observed_pytest) != 1 or len(declared_pytest) != 1:
        return False
    return _normalize_pytest_target(observed_pytest[0]) == _normalize_pytest_target(declared_pytest[0])


def _normalize_pytest_target(target: str) -> str:
    return target.replace("\\", "/").removeprefix("./")


def _pytest_args(command: tuple[str, ...]) -> tuple[str, ...] | None:
    if not command:
        return None
    executable = command[0].casefold()
    if executable in {"pytest", "pytest.exe"}:
        return command[1:]
    if len(command) >= 3 and executable in {"py", "py.exe", "python", "python.exe", "python3", "python3.exe"} and command[1] == "-m" and command[2].casefold() == "pytest":
        return command[3:]
    return None


def _work_id(*, project_id: str, component_id: str, repository_id: str, goal: str, base_sha: str, allowed_paths: tuple[str, ...], permission_ceiling: frozenset[str], acceptance_commands: tuple[tuple[str, ...], ...], attempt: int) -> str:
    return _stable_id("work", project_id, component_id, repository_id, goal, base_sha, allowed_paths, tuple(sorted(permission_ceiling)), acceptance_commands, attempt)


def _stable_id(prefix: str, *parts: object) -> str:
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return f"{prefix}-{hashlib.sha256(payload.encode()).hexdigest()[:24]}"


def _validate_work_request_scalar_authority(request: ComponentWorkRequest) -> None:
    if type(request) is not ComponentWorkRequest:
        raise CoordinatorError("work request must be an exact ComponentWorkRequest")
    text_values = (
        request.work_id,
        request.project_id,
        request.component_id,
        request.repository_id,
        request.goal,
    )
    if any(type(value) is not str for value in text_values):
        raise CoordinatorError("work request identity and goal must be exact strings")
    if not all(value.strip() for value in text_values):
        raise CoordinatorError("work request identity and goal must not be empty")
    _validate_sha(request.base_sha, "base_sha")
    if type(request.attempt) is not int or request.attempt < 1:
        raise CoordinatorError("attempt must be positive")


def _validate_worker_result_scalar_authority(envelope: WorkerResultEnvelope) -> None:
    if type(envelope) is not WorkerResultEnvelope:
        raise CoordinatorError("worker result must be an exact WorkerResultEnvelope")
    identity_values = (envelope.work_id, envelope.component_id, envelope.repository_id)
    if any(type(value) is not str for value in identity_values):
        raise CoordinatorError("worker result identity must be exact strings")
    if not all(value.strip() for value in identity_values):
        raise CoordinatorError("worker result identity must not be empty")
    _validate_sha(envelope.base_sha, "base_sha")
    _validate_sha(envelope.result_sha, "result_sha")
    _validate_digest(envelope.diff_digest, "diff_digest")
    if envelope.producer_actor_id is not None:
        _canonical_durable_text(
            envelope.producer_actor_id,
            label="producer actor id",
        )
    if type(envelope.coding_result) is not CodingResult:
        raise CoordinatorError("worker result coding result must be an exact CodingResult")
    if type(envelope.coding_result.job_id) is not str or not envelope.coding_result.job_id.strip():
        raise CoordinatorError("coding result job id must be an exact non-empty string")
    _validate_changed_files_carrier(envelope.coding_result.changed_files)
    _validate_test_evidence_carrier(envelope.coding_result.test_evidence)
    _validate_artifact_evidence_carrier(envelope.coding_result.artifacts)
    _validate_recovery_state_carrier(envelope.coding_result.recovery_state)
    if envelope.coding_result.failure is not None:
        _validate_worker_failure_carrier(envelope.coding_result.failure)


def _validate_changed_files_carrier(changed_files: object) -> None:
    if type(changed_files) is not tuple:
        raise CoordinatorError("changed files must be an exact tuple")
    for item in changed_files:
        if type(item) is not ChangedFile:
            raise CoordinatorError("changed file entries must be exact ChangedFile")
        if type(item.path) is not str or not item.path:
            raise CoordinatorError("changed file path must be an exact non-empty string")
        if (
            type(item.sha256) is not str
            or len(item.sha256) != 64
            or any(char not in "0123456789abcdef" for char in item.sha256.casefold())
        ):
            raise CoordinatorError("changed file sha256 must be an exact hexadecimal digest")
        if type(item.size_bytes) is not int or item.size_bytes < 0:
            raise CoordinatorError("changed file size must be an exact non-negative integer")


def _validate_test_evidence_carrier(evidence: object) -> None:
    if type(evidence) is not tuple:
        raise CoordinatorError("test evidence must be an exact tuple")
    for item in evidence:
        if type(item) is not TestEvidence:
            raise CoordinatorError("test evidence entries must be exact TestEvidence")
        if (
            type(item.command) is not tuple
            or not item.command
            or any(type(part) is not str or not part for part in item.command)
        ):
            raise CoordinatorError(
                "test evidence command must be an exact non-empty argv tuple"
            )
        if type(item.exit_code) is not int:
            raise CoordinatorError("test evidence exit code must be an exact integer")
        if type(item.output_digest) is not str:
            raise CoordinatorError(
                "test evidence output digest must be an exact string"
            )
        _canonical_durable_text(
            item.output_digest,
            label="test evidence output digest",
        )


def _validate_artifact_evidence_carrier(artifacts: object) -> None:
    if type(artifacts) is not tuple:
        raise CoordinatorError("artifact evidence must be an exact tuple")
    for item in artifacts:
        if type(item) is not ArtifactEvidence:
            raise CoordinatorError(
                "artifact evidence entries must be exact ArtifactEvidence"
            )
        values = (
            ("artifact evidence name", item.name),
            ("artifact evidence digest", item.digest),
            ("artifact evidence media type", item.media_type),
        )
        for label, value in values:
            if type(value) is not str:
                raise CoordinatorError(
                    "artifact evidence fields must be exact strings"
                )
            _canonical_durable_text(value, label=label)


def _validate_recovery_state_carrier(recovery_state: object) -> None:
    if recovery_state is None:
        return
    if type(recovery_state) is not RecoveryState:
        raise CoordinatorError("recovery state must be an exact RecoveryState")
    if type(recovery_state.phase) is not str:
        raise CoordinatorError("recovery state phase must be an exact string")
    _canonical_durable_text(recovery_state.phase, label="recovery state phase")
    if recovery_state.opaque_token is not None:
        if type(recovery_state.opaque_token) is not str:
            raise CoordinatorError("recovery state opaque token must be an exact string")
        _canonical_durable_text(
            recovery_state.opaque_token,
            label="recovery state opaque token",
        )


def _validate_worker_failure_carrier(failure: object) -> None:
    if type(failure) is not WorkerFailure:
        raise CoordinatorError("worker failure must be an exact WorkerFailure")
    if type(failure.kind) is not WorkerFailureKind:
        raise CoordinatorError("worker failure kind must be an exact WorkerFailureKind")
    if type(failure.retryable) is not bool:
        raise CoordinatorError("worker failure retryable must be an exact boolean")
    _canonical_worker_failure_message(failure.message)


def _validate_sha(value: object, label: str) -> None:
    if (
        type(value) is not str
        or len(value) != 40
        or any(char not in "0123456789abcdef" for char in value.casefold())
    ):
        raise CoordinatorError(f"{label} must be a 40-character hexadecimal SHA")


def _validate_digest(value: object, label: str) -> None:
    if (
        type(value) is not str
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value.casefold())
    ):
        raise CoordinatorError(f"{label} must be a 64-character hexadecimal digest")