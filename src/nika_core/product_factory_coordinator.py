from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.toolsmith.contracts import CodingResult, TestEvidence


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
        if not all(
            value.strip()
            for value in (self.work_id, self.project_id, self.component_id, self.repository_id, self.goal)
        ):
            raise CoordinatorError("work request identity and goal must not be empty")
        _validate_sha(self.base_sha, "base_sha")
        if not self.allowed_paths:
            raise CoordinatorError("work request must declare allowed paths")
        if not self.permission_ceiling:
            raise CoordinatorError("work request must declare a permission ceiling")
        if self.attempt < 1:
            raise CoordinatorError("attempt must be positive")


@dataclass(frozen=True, slots=True)
class WorkerResultEnvelope:
    work_id: str
    component_id: str
    repository_id: str
    base_sha: str
    result_sha: str
    diff_digest: str
    coding_result: CodingResult

    def __post_init__(self) -> None:
        if not all(value.strip() for value in (self.work_id, self.component_id, self.repository_id)):
            raise CoordinatorError("worker result identity must not be empty")
        _validate_sha(self.base_sha, "base_sha")
        _validate_sha(self.result_sha, "result_sha")
        _validate_digest(self.diff_digest, "diff_digest")


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    reviewer_id: str
    accepted: bool
    reason: str
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.reviewer_id.strip() or not self.reason.strip() or not self.evidence_refs:
            raise CoordinatorError("independent review requires reviewer, reason and evidence")


@dataclass(frozen=True, slots=True)
class WorkRecord:
    request: ComponentWorkRequest
    state: WorkState
    result: WorkerResultEnvelope | None = None
    review: ReviewDecision | None = None
    blocker: str | None = None


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
    _records: dict[str, WorkRecord] = field(default_factory=dict, init=False, repr=False)
    _revision: int = field(default=0, init=False, repr=False)
    _trusted_plan: tuple[ComponentWorkRequest, ...] | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _trusted_plan_fingerprint: str | None = field(default=None, init=False, repr=False)

    @property
    def trusted_plan_fingerprint(self) -> str:
        if self._trusted_plan_fingerprint is None:
            raise CoordinatorError("coordinator has no established trusted plan authority")
        return self._trusted_plan_fingerprint

    def plan(
        self,
        *,
        base_shas: dict[str, str],
        goals: dict[str, str],
        permission_ceiling: frozenset[str],
    ) -> CoordinatorSnapshot:
        if self._records or self._trusted_plan is not None:
            raise CoordinatorError("coordinator is already planned")
        if not permission_ceiling:
            raise CoordinatorError("project permission ceiling must not be empty")
        components = self._components()
        repositories = self._repositories()
        for component_id in self.graph.dependency_order():
            component = components[component_id]
            repository = repositories[component.repository_id]
            base_sha = base_shas.get(repository.repository_id)
            goal = goals.get(component_id, "").strip()
            if base_sha is None or not goal:
                raise CoordinatorError(f"missing base SHA or goal for component {component_id}")
            request = ComponentWorkRequest(
                work_id=_work_id(
                    project_id=self.graph.project_id,
                    component_id=component_id,
                    repository_id=repository.repository_id,
                    goal=goal,
                    base_sha=base_sha,
                    allowed_paths=component.paths,
                    permission_ceiling=permission_ceiling,
                    acceptance_commands=component.test_commands,
                    attempt=1,
                ),
                project_id=self.graph.project_id,
                component_id=component_id,
                repository_id=repository.repository_id,
                goal=goal,
                base_sha=base_sha,
                allowed_paths=component.paths,
                permission_ceiling=permission_ceiling,
                acceptance_commands=component.test_commands,
            )
            self._records[component_id] = WorkRecord(request=request, state=WorkState.PLANNED)
        self._trusted_plan = tuple(
            self._records[component_id].request for component_id in sorted(self._records)
        )
        self._trusted_plan_fingerprint = trusted_plan_fingerprint(self._trusted_plan)
        self._advance_ready()
        return self.snapshot()

    def ready_requests(self) -> tuple[ComponentWorkRequest, ...]:
        return tuple(
            record.request
            for _, record in sorted(self._records.items())
            if record.state is WorkState.READY
        )

    def start(self, component_id: str) -> ComponentWorkRequest:
        record = self._record(component_id)
        if record.state is not WorkState.READY:
            raise CoordinatorError(f"component {component_id} is not ready")
        self._records[component_id] = WorkRecord(record.request, WorkState.RUNNING)
        self._touch()
        return record.request

    def record_result(self, envelope: WorkerResultEnvelope) -> WorkRecord:
        record = self._record(envelope.component_id)
        if record.state is not WorkState.RUNNING:
            raise CoordinatorError("worker result is only valid for a running component")
        request = record.request
        self._validate_result_identity(request, envelope)
        if not envelope.coding_result.succeeded:
            blocker = envelope.coding_result.failure.message if envelope.coding_result.failure else None
            updated = WorkRecord(request, WorkState.REPAIR_REQUIRED, envelope, blocker=blocker)
        else:
            self._validate_success_evidence(request, envelope.coding_result.test_evidence)
            updated = WorkRecord(request, WorkState.REVIEW_REQUIRED, envelope)
        self._records[envelope.component_id] = updated
        self._touch()
        return updated

    def review(self, component_id: str, decision: ReviewDecision) -> WorkRecord:
        record = self._record(component_id)
        if record.state is not WorkState.REVIEW_REQUIRED or record.result is None:
            raise CoordinatorError("component is not awaiting independent review")
        state = WorkState.ACCEPTED if decision.accepted else WorkState.REPAIR_REQUIRED
        updated = WorkRecord(
            record.request,
            state,
            record.result,
            review=decision,
            blocker=None if decision.accepted else decision.reason,
        )
        self._records[component_id] = updated
        self._touch()
        self._advance_ready()
        return updated

    def prepare_repair(self, component_id: str, *, base_sha: str, reason: str) -> ComponentWorkRequest:
        record = self._record(component_id)
        if record.state is not WorkState.REPAIR_REQUIRED:
            raise CoordinatorError("repair can only be prepared from repair_required")
        if not reason.strip():
            raise CoordinatorError("repair reason must not be empty")
        attempt = record.request.attempt + 1
        goal = f"{record.request.goal}\nRepair: {reason}"
        request = ComponentWorkRequest(
            work_id=_work_id(
                project_id=self.graph.project_id,
                component_id=component_id,
                repository_id=record.request.repository_id,
                goal=goal,
                base_sha=base_sha,
                allowed_paths=record.request.allowed_paths,
                permission_ceiling=record.request.permission_ceiling,
                acceptance_commands=record.request.acceptance_commands,
                attempt=attempt,
            ),
            project_id=record.request.project_id,
            component_id=component_id,
            repository_id=record.request.repository_id,
            goal=goal,
            base_sha=base_sha,
            allowed_paths=record.request.allowed_paths,
            permission_ceiling=record.request.permission_ceiling,
            acceptance_commands=record.request.acceptance_commands,
            attempt=attempt,
        )
        self._records[component_id] = WorkRecord(request, WorkState.READY)
        self._touch()
        return request

    def block(self, component_id: str, reason: str) -> WorkRecord:
        if not reason.strip():
            raise CoordinatorError("blocker reason must not be empty")
        record = self._record(component_id)
        if record.state is WorkState.ACCEPTED:
            raise CoordinatorError("accepted component cannot be blocked")
        updated = WorkRecord(record.request, WorkState.BLOCKED, blocker=reason)
        self._records[component_id] = updated
        self._touch()
        return updated

    def snapshot(self) -> CoordinatorSnapshot:
        return CoordinatorSnapshot(
            self.graph.project_id,
            self._revision,
            tuple(self._records[key] for key in sorted(self._records)),
            self._trusted_plan,
        )

    def restore(
        self,
        snapshot: CoordinatorSnapshot,
        *,
        trusted_plan_fingerprint: str | None = None,
    ) -> None:
        authority = trusted_plan_fingerprint or self._trusted_plan_fingerprint
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
                raise CoordinatorError(
                    "snapshot work requests disagree on project permission ceiling"
                )

        for record in snapshot.records:
            request = record.request
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
            self._validate_restored_record(record)

        self._validate_restored_dependencies(snapshot.records)
        self._records = {record.request.component_id: record for record in snapshot.records}
        self._revision = snapshot.revision
        self._trusted_plan = plan
        self._trusted_plan_fingerprint = authority
        self._advance_ready()

    def _validate_restored_record(self, record: WorkRecord) -> None:
        request = record.request
        result = record.result
        review = record.review
        blocker = record.blocker

        if result is not None:
            self._validate_result_identity(request, result)

        if record.state in {WorkState.PLANNED, WorkState.READY, WorkState.RUNNING}:
            if result is not None or review is not None or blocker is not None:
                raise CoordinatorError("pre-result snapshot work contains terminal evidence")
            return

        if record.state is WorkState.REVIEW_REQUIRED:
            if (
                result is None
                or not result.coding_result.succeeded
                or review is not None
                or blocker is not None
            ):
                raise CoordinatorError(
                    "review_required snapshot work requires one successful result only"
                )
            self._validate_success_evidence(request, result.coding_result.test_evidence)
            return

        if record.state is WorkState.ACCEPTED:
            if (
                result is None
                or not result.coding_result.succeeded
                or review is None
                or not review.accepted
                or blocker is not None
            ):
                raise CoordinatorError(
                    "accepted snapshot work requires successful result and accepted review"
                )
            self._validate_success_evidence(request, result.coding_result.test_evidence)
            return

        if record.state is WorkState.REPAIR_REQUIRED:
            if result is None or not blocker:
                raise CoordinatorError(
                    "repair_required snapshot work requires result evidence and blocker"
                )
            if result.coding_result.succeeded:
                if review is None or review.accepted or blocker != review.reason:
                    raise CoordinatorError(
                        "review-rejected repair snapshot is internally inconsistent"
                    )
                self._validate_success_evidence(request, result.coding_result.test_evidence)
            elif review is not None:
                raise CoordinatorError(
                    "worker-failed repair snapshot cannot contain review evidence"
                )
            return

        if record.state is WorkState.BLOCKED:
            if result is not None or review is not None or not blocker:
                raise CoordinatorError(
                    "blocked snapshot work requires blocker without terminal evidence"
                )
            return

        raise CoordinatorError("snapshot contains unknown work state")

    def _validate_restored_dependencies(self, records: tuple[WorkRecord, ...]) -> None:
        accepted = {
            record.request.component_id
            for record in records
            if record.state is WorkState.ACCEPTED
        }
        components = self._components()
        states_requiring_accepted_dependencies = {
            WorkState.READY,
            WorkState.RUNNING,
            WorkState.REVIEW_REQUIRED,
            WorkState.ACCEPTED,
            WorkState.REPAIR_REQUIRED,
        }
        for record in records:
            if record.state not in states_requiring_accepted_dependencies:
                continue
            dependencies = set(components[record.request.component_id].dependencies)
            if not dependencies <= accepted:
                raise CoordinatorError(
                    "snapshot component state bypasses dependency acceptance"
                )

    @staticmethod
    def _validate_result_identity(
        request: ComponentWorkRequest,
        envelope: WorkerResultEnvelope,
    ) -> None:
        if envelope.component_id != request.component_id:
            raise CoordinatorError("worker result component does not match active request")
        if envelope.work_id != request.work_id or envelope.repository_id != request.repository_id:
            raise CoordinatorError("worker result identity does not match active request")
        if envelope.base_sha != request.base_sha:
            raise CoordinatorError("stale worker result base SHA does not match active request")
        if envelope.coding_result.job_id != request.work_id:
            raise CoordinatorError("coding result job id does not match Product Factory work id")

    @staticmethod
    def _validate_success_evidence(
        request: ComponentWorkRequest,
        evidence: tuple[TestEvidence, ...],
    ) -> None:
        if not evidence or any(item.exit_code != 0 for item in evidence):
            raise CoordinatorError("successful worker result requires passing test evidence")

        remaining = list(evidence)
        for declared in request.acceptance_commands:
            match_index = next(
                (
                    index
                    for index, item in enumerate(remaining)
                    if _commands_equivalent(
                        item.command,
                        declared,
                        component_id=request.component_id,
                    )
                ),
                None,
            )
            if match_index is None:
                raise CoordinatorError(
                    "successful worker result must prove every declared acceptance command"
                )
            remaining.pop(match_index)

    def _advance_ready(self) -> None:
        accepted = {key for key, item in self._records.items() if item.state is WorkState.ACCEPTED}
        components = self._components()
        changed = False
        for component_id, record in tuple(self._records.items()):
            if record.state is WorkState.PLANNED and set(components[component_id].dependencies) <= accepted:
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


def trusted_plan_fingerprint(plan: tuple[ComponentWorkRequest, ...]) -> str:
    if not plan:
        raise CoordinatorError("trusted plan descriptor must not be empty")
    payload = tuple(
        (
            request.project_id,
            request.component_id,
            request.repository_id,
            request.goal,
            request.base_sha,
            request.allowed_paths,
            tuple(sorted(request.permission_ceiling)),
            request.acceptance_commands,
        )
        for request in sorted(plan, key=lambda item: item.component_id)
    )
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_trusted_plan_snapshot(
    snapshot: CoordinatorSnapshot,
    authority_fingerprint: str,
) -> None:
    _validate_digest(authority_fingerprint, "trusted_plan_fingerprint")
    plan = snapshot.trusted_plan
    if plan is None or not plan:
        raise CoordinatorError("snapshot is missing immutable trusted plan descriptor")
    if trusted_plan_fingerprint(plan) != authority_fingerprint:
        raise CoordinatorError("snapshot trusted plan does not match external authority")

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
        expected_initial_work_id = _work_id(
            project_id=initial.project_id,
            component_id=initial.component_id,
            repository_id=initial.repository_id,
            goal=initial.goal,
            base_sha=initial.base_sha,
            allowed_paths=initial.allowed_paths,
            permission_ceiling=initial.permission_ceiling,
            acceptance_commands=initial.acceptance_commands,
            attempt=1,
        )
        if initial.work_id != expected_initial_work_id:
            raise CoordinatorError("trusted plan contains invalid attempt-one work identity")

    for record in snapshot.records:
        request = record.request
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

        expected_work_id = _work_id(
            project_id=request.project_id,
            component_id=request.component_id,
            repository_id=request.repository_id,
            goal=request.goal,
            base_sha=request.base_sha,
            allowed_paths=request.allowed_paths,
            permission_ceiling=request.permission_ceiling,
            acceptance_commands=request.acceptance_commands,
            attempt=request.attempt,
        )
        if request.work_id != expected_work_id:
            raise CoordinatorError("snapshot work id does not match durable request identity")


def _valid_repair_goal(initial_goal: str, current_goal: str, attempt: int) -> bool:
    if attempt <= 1 or not current_goal.startswith(initial_goal):
        return False
    suffix = current_goal[len(initial_goal) :]
    marker = "\nRepair: "
    if not suffix.startswith(marker):
        return False
    reasons = suffix.split(marker)[1:]
    return len(reasons) == attempt - 1 and all(reason.strip() for reason in reasons)


def _commands_equivalent(
    observed: tuple[str, ...],
    declared: tuple[str, ...],
    *,
    component_id: str,
) -> bool:
    if observed == declared:
        return True

    observed_pytest = _pytest_args(observed)
    declared_pytest = _pytest_args(declared)
    if observed_pytest is None or declared_pytest is None:
        return False
    if not observed_pytest:
        return True
    if observed_pytest == declared_pytest:
        return True
    if len(observed_pytest) != 1 or len(declared_pytest) != 1:
        return False

    observed_target = _normalize_pytest_target(observed_pytest[0])
    declared_target = _normalize_pytest_target(declared_pytest[0])
    return observed_target == declared_target


def _normalize_pytest_target(target: str) -> str:
    return target.replace("\\", "/").removeprefix("./")


def _pytest_args(command: tuple[str, ...]) -> tuple[str, ...] | None:
    if not command:
        return None
    executable = command[0].casefold()
    if executable in {"pytest", "pytest.exe"}:
        return command[1:]
    if (
        len(command) >= 3
        and executable in {"py", "py.exe", "python", "python.exe", "python3", "python3.exe"}
        and command[1] == "-m"
        and command[2].casefold() == "pytest"
    ):
        return command[3:]
    return None


def _work_id(
    *,
    project_id: str,
    component_id: str,
    repository_id: str,
    goal: str,
    base_sha: str,
    allowed_paths: tuple[str, ...],
    permission_ceiling: frozenset[str],
    acceptance_commands: tuple[tuple[str, ...], ...],
    attempt: int,
) -> str:
    return _stable_id(
        "work",
        project_id,
        component_id,
        repository_id,
        goal,
        base_sha,
        allowed_paths,
        tuple(sorted(permission_ceiling)),
        acceptance_commands,
        attempt,
    )


def _stable_id(prefix: str, *parts: object) -> str:
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return f"{prefix}-{hashlib.sha256(payload.encode()).hexdigest()[:24]}"


def _validate_sha(value: str, label: str) -> None:
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value.casefold()):
        raise CoordinatorError(f"{label} must be a 40-character hexadecimal SHA")


def _validate_digest(value: str, label: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value.casefold()):
        raise CoordinatorError(f"{label} must be a 64-character hexadecimal digest")
