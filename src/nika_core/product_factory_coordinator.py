from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from nika_core.product_factory_orchestration import ProductRepositoryGraph
from nika_core.toolsmith.contracts import CodingResult, TestEvidence


class CoordinatorError(ValueError):
    """Raised when Product Factory orchestration invariants are violated."""


class WorkState(StrEnum):
    PLANNED = "planned"
    READY = "ready"
    RUNNING = "running"
    REVIEW_REQUIRED = "review_required"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
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
        if not self.reviewer_id.strip() or not self.reason.strip():
            raise CoordinatorError("review identity and reason must not be empty")
        if not self.evidence_refs:
            raise CoordinatorError("review requires evidence")


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
    """Deterministic PF4 coordinator above bounded component work.

    It does not own an agent runtime or CodingWorker implementation. It only decides
    which graph components are dependency-ready, validates exact worker evidence and
    drives review/repair/restart state.
    """

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

        for component_id in self.graph.dependency_order():
            component = self.graph.component(component_id)
            repository = self.graph.repository(component.repository_id)
            base_sha = base_shas.get(repository.repository_id)
            goal = goals.get(component_id, "").strip()
            if base_sha is None or not goal:
                raise CoordinatorError(f"missing base SHA or goal for component {component_id}")
            _validate_sha(base_sha, f"base SHA for {repository.repository_id}")
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
            for component_id, record in sorted(self._records.items())
            if record.state is WorkState.READY
        )

    def start(self, component_id: str) -> ComponentWorkRequest:
        record = self._record(component_id)
        if record.state is not WorkState.READY:
            raise CoordinatorError(f"component {component_id} is not ready")
        self._records[component_id] = WorkRecord(request=record.request, state=WorkState.RUNNING)
        self._touch()
        return record.request

    def record_result(self, envelope: WorkerResultEnvelope) -> WorkRecord:
        record = self._record(envelope.component_id)
        if record.state is not WorkState.RUNNING:
            raise CoordinatorError("worker result is only valid for a running component")
        request = record.request
        if envelope.work_id != request.work_id or envelope.repository_id != request.repository_id:
            raise CoordinatorError("worker result identity does not match active request")
        if envelope.base_sha != request.base_sha:
            raise CoordinatorError("stale worker result base SHA does not match active request")
        if envelope.coding_result.job_id != request.work_id:
            raise CoordinatorError("coding result job id does not match Product Factory work id")
        if not envelope.coding_result.succeeded:
            updated = WorkRecord(
                request=request,
                state=WorkState.REPAIR_REQUIRED,
                result=envelope,
                blocker=envelope.coding_result.failure.message if envelope.coding_result.failure else None,
            )
        elif not envelope.coding_result.test_evidence:
            raise CoordinatorError("successful worker result requires test evidence")
        elif any(item.exit_code != 0 for item in envelope.coding_result.test_evidence):
            raise CoordinatorError("successful worker result contains failing test evidence")
        else:
            updated = WorkRecord(request=request, state=WorkState.REVIEW_REQUIRED, result=envelope)
        self._records[envelope.component_id] = updated
        self._touch()
        return updated

    def review(self, component_id: str, decision: ReviewDecision) -> WorkRecord:
        record = self._record(component_id)
        if record.state is not WorkState.REVIEW_REQUIRED or record.result is None:
            raise CoordinatorError("component is not awaiting independent review")
        state = WorkState.ACCEPTED if decision.accepted else WorkState.REPAIR_REQUIRED
        updated = WorkRecord(
            request=record.request,
            state=state,
            result=record.result,
            review=decision,
            blocker=None if decision.accepted else decision.reason,
        )
        self._records[component_id] = updated
        self._touch()
        self._advance_ready()
        return updated

    def prepare_repair(self, component_id: str, *, base_sha: str, reason: str) -> ComponentWorkRequest:
        record = self._record(component_id)
        if record.state not in {WorkState.REPAIR_REQUIRED, WorkState.REJECTED}:
            raise CoordinatorError("repair can only be prepared from a rejected/repair state")
        if not reason.strip():
            raise CoordinatorError("repair reason must not be empty")
        _validate_sha(base_sha, "repair base_sha")
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
        self._records[component_id] = WorkRecord(request=request, state=WorkState.READY)
        self._touch()
        return request

    def block(self, component_id: str, reason: str) -> WorkRecord:
        if not reason.strip():
            raise CoordinatorError("blocker reason must not be empty")
        record = self._record(component_id)
        if record.state is WorkState.ACCEPTED:
            raise CoordinatorError("accepted component cannot be blocked")
        updated = WorkRecord(request=record.request, state=WorkState.BLOCKED, blocker=reason)
        self._records[component_id] = updated
        self._touch()
        self._advance_ready()
        return updated

    def snapshot(self) -> CoordinatorSnapshot:
        return CoordinatorSnapshot(
            project_id=self.graph.project_id,
            revision=self._revision,
            records=tuple(self._records[key] for key in sorted(self._records)),
        )

    def restore(self, snapshot: CoordinatorSnapshot) -> None:
        if snapshot.project_id != self.graph.project_id:
            raise CoordinatorError("snapshot project does not match repository graph")
        expected = set(self.graph.component_ids())
        actual = {record.request.component_id for record in snapshot.records}
        if actual != expected:
            raise CoordinatorError("snapshot component set does not match repository graph")
        if len(actual) != len(snapshot.records):
            raise CoordinatorError("snapshot contains duplicate component records")
        self._records = {record.request.component_id: record for record in snapshot.records}
        self._revision = snapshot.revision
        self._advance_ready()

    def _advance_ready(self) -> None:
        changed = False
        accepted = {
            component_id
            for component_id, record in self._records.items()
            if record.state is WorkState.ACCEPTED
        }
        for component_id, record in tuple(self._records.items()):
            if record.state is not WorkState.PLANNED:
                continue
            component = self.graph.component(component_id)
            if set(component.dependencies) <= accepted:
                self._records[component_id] = WorkRecord(
                    request=record.request,
                    state=WorkState.READY,
                )
                changed = True
        if changed:
            self._touch()

    def _record(self, component_id: str) -> WorkRecord:
        try:
            return self._records[component_id]
        except KeyError as exc:
            raise CoordinatorError(f"unknown component {component_id}") from exc

    def _touch(self) -> None:
        self._revision += 1


def validate_test_evidence(evidence: tuple[TestEvidence, ...]) -> None:
    if not evidence:
        raise CoordinatorError("test evidence must not be empty")
    if any(item.exit_code != 0 for item in evidence):
        raise CoordinatorError("test evidence contains a failing command")


def _stable_id(prefix: str, *parts: object) -> str:
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


def _validate_sha(value: str, label: str) -> None:
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value.casefold()):
        raise CoordinatorError(f"{label} must be a 40-character hexadecimal SHA")


def _validate_digest(value: str, label: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value.casefold()):
        raise CoordinatorError(f"{label} must be a 64-character hexadecimal digest")
