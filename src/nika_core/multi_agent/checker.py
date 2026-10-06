from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from nika_core.multi_agent.contracts import AgentHandoff, HandoffKind
from nika_core.multi_agent.research_results import (
    SourceInspectionAssignment,
    SourceResultBindingError,
    decode_source_result,
)
from nika_core.runtime.contracts import RuntimeErrorCode

_SAFE_WORKER_ERRORS = frozenset(
    {
        "AssertionError",
        "KeyError",
        "OSError",
        "PermissionError",
        "RuntimeError",
        "RuntimeFailure",
        "TimeoutError",
        "TypeError",
        "ValueError",
        "WorkerException",
        *(item.value for item in RuntimeErrorCode),
    }
)


class CheckerStatus(StrEnum):
    AGREE = "agree"
    DISAGREE = "disagree"
    PARTIAL = "partial"
    MISSING = "missing"
    WORKER_ERROR = "worker_error"
    EVIDENCE_INVALID = "evidence_invalid"


class CheckerSourceState(StrEnum):
    VALID = "valid"
    MISSING = "missing"
    WORKER_ERROR = "worker_error"
    EVIDENCE_INVALID = "evidence_invalid"


@dataclass(frozen=True, slots=True)
class CheckerValue:
    source_id: str
    present: bool
    value: Any = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"source_id": self.source_id, "present": self.present}
        if self.present:
            payload["value"] = _json_clone(self.value, name="comparison value")
        return payload


@dataclass(frozen=True, slots=True)
class CheckerDifference:
    field: str
    values: tuple[CheckerValue, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "values": [value.to_payload() for value in self.values],
        }


@dataclass(frozen=True, slots=True)
class CheckerSourceSummary:
    source_id: str
    worker_id: str
    assignment_id: str
    state: CheckerSourceState
    handoff_id: str | None = None
    correlation_id: str | None = None
    result_digest: str | None = None
    result_set: dict[str, object] | None = None
    error: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source_id": self.source_id,
            "worker_id": self.worker_id,
            "assignment_id": self.assignment_id,
            "state": self.state.value,
        }
        if self.handoff_id is not None:
            payload["handoff_id"] = self.handoff_id
        if self.correlation_id is not None:
            payload["correlation_id"] = self.correlation_id
        if self.result_digest is not None:
            payload["result_digest"] = self.result_digest
        if self.result_set is not None:
            payload["result_set"] = _json_clone(self.result_set, name="result_set")
        if self.error is not None:
            payload["error"] = self.error
        return payload


@dataclass(frozen=True, slots=True)
class CheckerSummary:
    team_id: str
    task_id: str
    checker_id: str
    status: CheckerStatus
    sources: tuple[CheckerSourceSummary, ...]
    agreements: tuple[str, ...]
    differences: tuple[CheckerDifference, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "nika.v01.checker-summary:v2",
            "team_id": self.team_id,
            "task_id": self.task_id,
            "checker_id": self.checker_id,
            "status": self.status.value,
            "comparison_domain": "canonical-research-items:v1",
            "sources": [source.to_payload() for source in self.sources],
            "agreements": list(self.agreements),
            "differences": [difference.to_payload() for difference in self.differences],
        }


class V01CheckerAgent:
    """Fail-closed deterministic comparison of exactly two source-bound M7 results."""

    def compare(
        self,
        *,
        team_id: str,
        task_id: str,
        checker_id: str,
        assignments: tuple[SourceInspectionAssignment, ...],
        handoffs: tuple[AgentHandoff, ...],
    ) -> CheckerSummary:
        _require_identifier(team_id, "team_id")
        _require_identifier(task_id, "task_id")
        _require_identifier(checker_id, "checker_id")
        self._validate_assignments(
            team_id=team_id,
            task_id=task_id,
            checker_id=checker_id,
            assignments=assignments,
        )

        indexed, invalid_workers, global_invalid = self._index_handoffs(
            team_id=team_id,
            checker_id=checker_id,
            assignments=assignments,
            handoffs=handoffs,
        )
        sources = tuple(
            self._source_summary(
                assignment,
                indexed.get(assignment.member_id),
                force_invalid=global_invalid or assignment.member_id in invalid_workers,
            )
            for assignment in assignments
        )

        states = {source.state for source in sources}
        if CheckerSourceState.EVIDENCE_INVALID in states:
            return self._noncomparison_summary(
                team_id, task_id, checker_id, CheckerStatus.EVIDENCE_INVALID, sources
            )
        if CheckerSourceState.WORKER_ERROR in states:
            return self._noncomparison_summary(
                team_id, task_id, checker_id, CheckerStatus.WORKER_ERROR, sources
            )
        if CheckerSourceState.MISSING in states:
            return self._noncomparison_summary(
                team_id, task_id, checker_id, CheckerStatus.MISSING, sources
            )

        status, agreements, differences = self._compare_valid_sources(sources)
        return CheckerSummary(
            team_id=team_id,
            task_id=task_id,
            checker_id=checker_id,
            status=status,
            sources=sources,
            agreements=agreements,
            differences=differences,
        )

    @staticmethod
    def _validate_assignments(
        *,
        team_id: str,
        task_id: str,
        checker_id: str,
        assignments: tuple[SourceInspectionAssignment, ...],
    ) -> None:
        if len(assignments) != 2:
            raise ValueError("V0.1 checker requires exactly two source assignments")
        if any(item.team_id != team_id for item in assignments):
            raise ValueError("checker assignment belongs to a different team")
        if any(item.task_id != task_id for item in assignments):
            raise ValueError("checker assignment belongs to a different task")
        worker_ids = [item.member_id for item in assignments]
        source_ids = [item.source.source_id for item in assignments]
        assignment_ids = [item.assignment_id for item in assignments]
        if checker_id in worker_ids:
            raise ValueError("checker identity must differ from worker identities")
        for label, values in (
            ("worker", worker_ids),
            ("source", source_ids),
            ("assignment", assignment_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"checker {label} identities must be unique")

    @staticmethod
    def _index_handoffs(
        *,
        team_id: str,
        checker_id: str,
        assignments: tuple[SourceInspectionAssignment, ...],
        handoffs: tuple[AgentHandoff, ...],
    ) -> tuple[dict[str, AgentHandoff], set[str], bool]:
        expected = {assignment.member_id for assignment in assignments}
        indexed: dict[str, AgentHandoff] = {}
        invalid_workers: set[str] = set()
        global_invalid = False
        for handoff in handoffs:
            if (
                handoff.team_id != team_id
                or handoff.recipient_id != checker_id
                or handoff.kind not in {HandoffKind.RESULT, HandoffKind.ERROR}
                or handoff.sender_id not in expected
            ):
                global_invalid = True
                continue
            expected_correlation = f"team:{team_id}:{checker_id}:{handoff.sender_id}"
            if handoff.correlation_id != expected_correlation:
                invalid_workers.add(handoff.sender_id)
            if handoff.sender_id in indexed:
                invalid_workers.add(handoff.sender_id)
            else:
                indexed[handoff.sender_id] = handoff
        return indexed, invalid_workers, global_invalid

    @staticmethod
    def _source_summary(
        assignment: SourceInspectionAssignment,
        handoff: AgentHandoff | None,
        *,
        force_invalid: bool,
    ) -> CheckerSourceSummary:
        base = {
            "source_id": assignment.source.source_id,
            "worker_id": assignment.member_id,
            "assignment_id": assignment.assignment_id,
        }
        if force_invalid:
            return CheckerSourceSummary(
                **base,
                state=CheckerSourceState.EVIDENCE_INVALID,
                error="handoff_identity_invalid",
            )
        if handoff is None:
            return CheckerSourceSummary(**base, state=CheckerSourceState.MISSING)
        if handoff.kind is HandoffKind.ERROR:
            raw_error = handoff.payload.get("error")
            return CheckerSourceSummary(
                **base,
                state=CheckerSourceState.WORKER_ERROR,
                handoff_id=handoff.handoff_id,
                correlation_id=handoff.correlation_id,
                error=_safe_worker_error(raw_error),
            )

        try:
            decode_source_result(
                assignment,
                member_id=handoff.sender_id,
                output=handoff.payload,
            )
            result_set = handoff.payload["result_set"]
            result_digest = handoff.payload["result_digest"]
            if not isinstance(result_set, dict) or not isinstance(result_digest, str):
                raise SourceResultBindingError("validated result payload is malformed")
            return CheckerSourceSummary(
                **base,
                state=CheckerSourceState.VALID,
                handoff_id=handoff.handoff_id,
                correlation_id=handoff.correlation_id,
                result_digest=result_digest,
                result_set=_json_clone(result_set, name="result_set"),
            )
        except (KeyError, SourceResultBindingError, TypeError, ValueError):
            return CheckerSourceSummary(
                **base,
                state=CheckerSourceState.EVIDENCE_INVALID,
                handoff_id=handoff.handoff_id,
                correlation_id=handoff.correlation_id,
                error="source_result_binding_invalid",
            )

    @staticmethod
    def _noncomparison_summary(
        team_id: str,
        task_id: str,
        checker_id: str,
        status: CheckerStatus,
        sources: tuple[CheckerSourceSummary, ...],
    ) -> CheckerSummary:
        return CheckerSummary(
            team_id=team_id,
            task_id=task_id,
            checker_id=checker_id,
            status=status,
            sources=sources,
            agreements=(),
            differences=(),
        )

    @staticmethod
    def _compare_valid_sources(
        sources: tuple[CheckerSourceSummary, ...],
    ) -> tuple[CheckerStatus, tuple[str, ...], tuple[CheckerDifference, ...]]:
        if len(sources) != 2 or any(
            item.state is not CheckerSourceState.VALID or item.result_set is None
            for item in sources
        ):
            raise ValueError("complete comparison requires two valid source results")
        left, right = sources
        assert left.result_set is not None and right.result_set is not None
        left_projection = _comparison_projection(left.result_set)
        right_projection = _comparison_projection(right.result_set)
        if left_projection == right_projection:
            return CheckerStatus.AGREE, ("result_set",), ()

        left_items = set(_item_fingerprints(left.result_set))
        right_items = set(_item_fingerprints(right.result_set))
        shared = tuple(sorted(left_items & right_items))
        status = CheckerStatus.PARTIAL if shared else CheckerStatus.DISAGREE
        agreements = tuple(f"item:{digest}" for digest in shared)
        difference = CheckerDifference(
            field="result_set",
            values=(
                CheckerValue(
                    source_id=left.source_id,
                    present=True,
                    value=left_projection,
                ),
                CheckerValue(
                    source_id=right.source_id,
                    present=True,
                    value=right_projection,
                ),
            ),
        )
        return status, agreements, (difference,)


def _comparison_projection(result_set: Mapping[str, object]) -> dict[str, object]:
    items = result_set.get("items")
    if not isinstance(items, list):
        raise TypeError("validated result_set items must be a list")
    query = result_set.get("query")
    if not isinstance(query, str):
        raise TypeError("validated result_set query must be text")
    return {
        "query": query,
        "items": sorted(_item_fingerprints(result_set)),
    }


def _item_fingerprints(result_set: Mapping[str, object]) -> tuple[str, ...]:
    items = result_set.get("items")
    if not isinstance(items, list):
        raise TypeError("validated result_set items must be a list")
    digests: list[str] = []
    for raw_item in items:
        if not isinstance(raw_item, Mapping):
            raise TypeError("validated result item must be an object")
        comparable = {
            key: raw_item[key]
            for key in ("title", "snippet", "rank", "why_matched")
        }
        encoded = json.dumps(
            comparable,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digests.append(hashlib.sha256(encoded).hexdigest())
    return tuple(sorted(digests))


def _json_clone(value: Any, *, name: str) -> Any:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{name} must be JSON-compatible") from exc
    return json.loads(encoded)


def _require_identifier(value: str, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be text")
    if not value.strip():
        raise ValueError(f"{name} must not be empty")
    if value != value.strip():
        raise ValueError(f"{name} must not contain surrounding whitespace")


def _safe_worker_error(value: object) -> str:
    if isinstance(value, str) and value in _SAFE_WORKER_ERRORS:
        return value
    return "WorkerFailure"
