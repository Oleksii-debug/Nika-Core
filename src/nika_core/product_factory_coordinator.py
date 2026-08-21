from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.toolsmith.contracts import CodingResult


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


class ComponentDispatcherPort(Protocol):
    async def dispatch(self, request: ComponentWorkRequest) -> WorkerResultEnvelope: ...


@dataclass(slots=True)
class ProductFactoryCoordinator:
    """PF4 coordinator above bounded component work, not a second agent runtime."""

    graph: ProductRepositoryGraph
    _records: dict[str, WorkRecord] = field(default_factory=dict, init=False, repr=False)
    _revision: int = field(default=0, init=False, repr=False)

    def plan(
        self,
        *,
        base_shas: dict[str, str],
        goals: dict[str, str],
        permission_ceiling: frozenset[str],
    ) -> CoordinatorSnapshot:
        if self._records:
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
                work_id=_stable_id("work", self.graph.project_id, component_id, 1),
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
            self._validate_success_evidence(request, envelope.coding_result)
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
        request = ComponentWorkRequest(
            work_id=_stable_id("work", self.graph.project_id, component_id, attempt),
            project_id=record.request.project_id,
            component_id=component_id,
            repository_id=record.request.repository_id,
            goal=f"{record.request.goal}\nRepair: {reason}",
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
        )

    def restore(self, snapshot: CoordinatorSnapshot) -> None:
        if snapshot.project_id != self.graph.project_id:
            raise CoordinatorError("snapshot project does not match repository graph")
        if snapshot.revision < 0:
            raise CoordinatorError("snapshot revision must not be negative")
        components = self._components()
        expected = set(components)
        actual = [record.request.component_id for record in snapshot.records]
        if set(actual) != expected or len(actual) != len(set(actual)):
            raise CoordinatorError("snapshot component set does not match repository graph")
        permission_ceilings = {record.request.permission_ceiling for record in snapshot.records}
        if len(permission_ceilings) > 1:
            raise CoordinatorError("snapshot permission ceiling is inconsistent across components")
        for record in snapshot.records:
            self._validate_restored_record(record, components[record.request.component_id])
        self._records = {record.request.component_id: record for record in snapshot.records}
        self._revision = snapshot.revision
        self._advance_ready()

    def _validate_restored_record(self, record: WorkRecord, component: ProductComponent) -> None:
        request = record.request
        if request.project_id != self.graph.project_id:
            raise CoordinatorError("snapshot work request project does not match repository graph")
        if request.component_id != component.component_id:
            raise CoordinatorError("snapshot work request component identity does not match graph")
        if request.repository_id != component.repository_id:
            raise CoordinatorError("snapshot work request repository does not match component ownership")
        if request.allowed_paths != component.paths:
            raise CoordinatorError("snapshot work request paths exceed component ownership")
        if request.acceptance_commands != component.test_commands:
            raise CoordinatorError("snapshot acceptance commands do not match component contract")
        expected_work_id = _stable_id(
            "work", self.graph.project_id, component.component_id, request.attempt
        )
        if request.work_id != expected_work_id:
            raise CoordinatorError("snapshot work id does not match deterministic attempt identity")

        if record.result is not None:
            self._validate_result_identity(request, record.result)
            if record.result.coding_result.succeeded:
                self._validate_success_evidence(request, record.result.coding_result)

        if record.state in {WorkState.PLANNED, WorkState.READY, WorkState.RUNNING}:
            if record.result is not None or record.review is not None or record.blocker is not None:
                raise CoordinatorError("pre-result work state contains impossible result/review data")
            return
        if record.state is WorkState.REVIEW_REQUIRED:
            if (
                record.result is None
                or not record.result.coding_result.succeeded
                or record.review is not None
                or record.blocker is not None
            ):
                raise CoordinatorError("review_required snapshot record is internally inconsistent")
            return
        if record.state is WorkState.ACCEPTED:
            if (
                record.result is None
                or not record.result.coding_result.succeeded
                or record.review is None
                or not record.review.accepted
                or record.blocker is not None
            ):
                raise CoordinatorError("accepted snapshot record lacks accepted result and review")
            return
        if record.state is WorkState.REPAIR_REQUIRED:
            self._validate_repair_record(record)
            return
        if record.state is WorkState.BLOCKED:
            if record.result is not None or record.review is not None or not _nonempty(record.blocker):
                raise CoordinatorError("blocked snapshot record requires only a durable blocker")
            return
        raise CoordinatorError(f"unsupported snapshot work state: {record.state}")

    def _validate_repair_record(self, record: WorkRecord) -> None:
        result = record.result
        if result is None:
            raise CoordinatorError("repair_required snapshot record must retain worker/review evidence")
        if not result.coding_result.succeeded:
            if record.review is not None:
                raise CoordinatorError("failed worker result cannot carry an independent review")
            return
        if record.review is None or record.review.accepted or not _nonempty(record.blocker):
            raise CoordinatorError("successful repair_required result requires a rejected review")

    def _validate_result_identity(
        self, request: ComponentWorkRequest, envelope: WorkerResultEnvelope
    ) -> None:
        if (
            envelope.work_id != request.work_id
            or envelope.component_id != request.component_id
            or envelope.repository_id != request.repository_id
        ):
            raise CoordinatorError("worker result identity does not match active request")
        if envelope.base_sha != request.base_sha:
            raise CoordinatorError("stale worker result base SHA does not match active request")
        if envelope.coding_result.job_id != request.work_id:
            raise CoordinatorError("coding result job id does not match Product Factory work id")

    def _validate_success_evidence(
        self, request: ComponentWorkRequest, coding_result: CodingResult
    ) -> None:
        evidence = coding_result.test_evidence
        if not evidence or any(item.exit_code != 0 for item in evidence):
            raise CoordinatorError("successful worker result requires passing test evidence")
        required = Counter(request.acceptance_commands)
        observed = Counter(item.command for item in evidence)
        missing = required - observed
        if missing:
            raise CoordinatorError("successful worker result is missing declared acceptance evidence")

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


def _stable_id(prefix: str, *parts: object) -> str:
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return f"{prefix}-{hashlib.sha256(payload.encode()).hexdigest()[:24]}"


def _validate_sha(value: str, label: str) -> None:
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value.casefold()):
        raise CoordinatorError(f"{label} must be a 40-character hexadecimal SHA")


def _validate_digest(value: str, label: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value.casefold()):
        raise CoordinatorError(f"{label} must be a 64-character hexadecimal digest")


def _nonempty(value: str | None) -> bool:
    return value is not None and bool(value.strip())
