from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from nika_core.multi_agent.contracts import AgentHandoff, HandoffKind

_MISSING = object()


class CheckerStatus(StrEnum):
    AGREEMENT = "agreement"
    DIFFERENCE = "difference"
    MISSING_RESULT = "missing_result"


class CheckerSourceState(StrEnum):
    RESULT = "result"
    ERROR = "error"
    MISSING_RESULT = "missing_result"


@dataclass(frozen=True, slots=True)
class CheckerSource:
    source_id: str
    worker_id: str

    def __post_init__(self) -> None:
        _require_identifier(self.source_id, "source_id")
        _require_identifier(self.worker_id, "worker_id")


@dataclass(frozen=True, slots=True)
class CheckerValue:
    source_id: str
    present: bool
    value: Any = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source_id": self.source_id,
            "present": self.present,
        }
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
    state: CheckerSourceState
    handoff_id: str | None = None
    correlation_id: str | None = None
    facts: dict[str, Any] | None = None
    result_present: bool = False
    result: Any = None
    error: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source_id": self.source_id,
            "worker_id": self.worker_id,
            "state": self.state.value,
        }
        if self.handoff_id is not None:
            payload["handoff_id"] = self.handoff_id
        if self.correlation_id is not None:
            payload["correlation_id"] = self.correlation_id
        if self.facts is not None:
            payload["facts"] = _json_clone(self.facts, name="facts")
        if self.result_present:
            payload["result"] = _json_clone(self.result, name="result")
        if self.error is not None:
            payload["error"] = self.error
        return payload


@dataclass(frozen=True, slots=True)
class CheckerSummary:
    team_id: str
    checker_id: str
    status: CheckerStatus
    sources: tuple[CheckerSourceSummary, ...]
    agreements: tuple[str, ...]
    differences: tuple[CheckerDifference, ...]
    missing_sources: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "nika.v01.checker_summary.v1",
            "team_id": self.team_id,
            "checker_id": self.checker_id,
            "status": self.status.value,
            "sources": [source.to_payload() for source in self.sources],
            "agreements": list(self.agreements),
            "differences": [difference.to_payload() for difference in self.differences],
            "missing_sources": list(self.missing_sources),
        }


class V01CheckerAgent:
    """Deterministic two-source checker over existing typed M7 handoffs.

    The checker does not grant tools, schedule work, mutate team state, or decide
    Product Factory/release authority. Model-assisted worker extraction may happen
    through the existing ModelGateway before this boundary; comparison itself is
    intentionally deterministic and remains valid in the no-model CI route.
    """

    def compare(
        self,
        *,
        team_id: str,
        checker_id: str,
        sources: tuple[CheckerSource, ...],
        handoffs: tuple[AgentHandoff, ...],
    ) -> CheckerSummary:
        _require_identifier(team_id, "team_id")
        _require_identifier(checker_id, "checker_id")
        self._validate_sources(sources)

        by_worker = self._index_handoffs(team_id=team_id, sources=sources, handoffs=handoffs)
        snapshots = tuple(
            self._source_summary(source, by_worker.get(source.worker_id)) for source in sources
        )
        missing_sources = tuple(
            item.source_id for item in snapshots if item.state is not CheckerSourceState.RESULT
        )
        if missing_sources:
            return CheckerSummary(
                team_id=team_id,
                checker_id=checker_id,
                status=CheckerStatus.MISSING_RESULT,
                sources=snapshots,
                agreements=(),
                differences=(),
                missing_sources=missing_sources,
            )

        agreements, differences = self._compare_complete_sources(snapshots)
        return CheckerSummary(
            team_id=team_id,
            checker_id=checker_id,
            status=(CheckerStatus.DIFFERENCE if differences else CheckerStatus.AGREEMENT),
            sources=snapshots,
            agreements=agreements,
            differences=differences,
            missing_sources=(),
        )

    @staticmethod
    def _validate_sources(sources: tuple[CheckerSource, ...]) -> None:
        if len(sources) != 2:
            raise ValueError("V0.1 checker requires exactly two declared sources")
        source_ids = [source.source_id for source in sources]
        worker_ids = [source.worker_id for source in sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("checker source_id values must be unique")
        if len(worker_ids) != len(set(worker_ids)):
            raise ValueError("checker worker_id values must be unique")

    @staticmethod
    def _index_handoffs(
        *,
        team_id: str,
        sources: tuple[CheckerSource, ...],
        handoffs: tuple[AgentHandoff, ...],
    ) -> dict[str, AgentHandoff]:
        expected = {source.worker_id: source for source in sources}
        by_worker: dict[str, AgentHandoff] = {}
        for handoff in handoffs:
            if handoff.team_id != team_id:
                raise ValueError("checker handoff belongs to a different team")
            if handoff.kind not in {HandoffKind.RESULT, HandoffKind.ERROR}:
                raise ValueError("checker accepts only RESULT or ERROR worker handoffs")
            source = expected.get(handoff.sender_id)
            if source is None:
                raise ValueError(
                    f"checker received output from undeclared worker: {handoff.sender_id}"
                )
            if handoff.sender_id in by_worker:
                raise ValueError(f"checker received duplicate worker output: {handoff.sender_id}")
            payload_source = handoff.payload.get("source_id")
            if payload_source is not None and payload_source != source.source_id:
                raise ValueError(
                    f"worker output source mismatch for {handoff.sender_id}: "
                    f"expected {source.source_id}"
                )
            by_worker[handoff.sender_id] = handoff
        return by_worker

    @staticmethod
    def _source_summary(
        source: CheckerSource,
        handoff: AgentHandoff | None,
    ) -> CheckerSourceSummary:
        if handoff is None:
            return CheckerSourceSummary(
                source_id=source.source_id,
                worker_id=source.worker_id,
                state=CheckerSourceState.MISSING_RESULT,
            )

        if handoff.kind is HandoffKind.ERROR:
            raw_error = handoff.payload.get("error")
            if raw_error is not None and not isinstance(raw_error, str):
                raise TypeError("worker error must be text when provided")
            return CheckerSourceSummary(
                source_id=source.source_id,
                worker_id=source.worker_id,
                state=CheckerSourceState.ERROR,
                handoff_id=handoff.handoff_id,
                correlation_id=handoff.correlation_id,
                error=raw_error,
            )

        raw_facts = handoff.payload.get("facts", {})
        if not isinstance(raw_facts, dict):
            raise TypeError("worker facts must be an object")
        facts = _normalize_facts(raw_facts)
        if "result" not in handoff.payload:
            return CheckerSourceSummary(
                source_id=source.source_id,
                worker_id=source.worker_id,
                state=CheckerSourceState.MISSING_RESULT,
                handoff_id=handoff.handoff_id,
                correlation_id=handoff.correlation_id,
                facts=facts,
            )

        result = _json_clone(handoff.payload["result"], name="worker result")
        return CheckerSourceSummary(
            source_id=source.source_id,
            worker_id=source.worker_id,
            state=CheckerSourceState.RESULT,
            handoff_id=handoff.handoff_id,
            correlation_id=handoff.correlation_id,
            facts=facts,
            result_present=True,
            result=result,
        )

    @staticmethod
    def _compare_complete_sources(
        sources: tuple[CheckerSourceSummary, ...],
    ) -> tuple[tuple[str, ...], tuple[CheckerDifference, ...]]:
        if len(sources) != 2 or any(
            source.state is not CheckerSourceState.RESULT for source in sources
        ):
            raise ValueError("complete comparison requires exactly two worker results")

        left, right = sources
        left_facts = left.facts or {}
        right_facts = right.facts or {}
        agreements: list[str] = []
        differences: list[CheckerDifference] = []

        for key in sorted(set(left_facts) | set(right_facts)):
            left_value = left_facts.get(key, _MISSING)
            right_value = right_facts.get(key, _MISSING)
            field = f"facts.{key}"
            if (
                left_value is not _MISSING
                and right_value is not _MISSING
                and left_value == right_value
            ):
                agreements.append(field)
                continue
            differences.append(
                CheckerDifference(
                    field=field,
                    values=(
                        _comparison_value(left.source_id, left_value),
                        _comparison_value(right.source_id, right_value),
                    ),
                )
            )

        if left.result == right.result:
            agreements.append("result")
        else:
            differences.append(
                CheckerDifference(
                    field="result",
                    values=(
                        CheckerValue(source_id=left.source_id, present=True, value=left.result),
                        CheckerValue(source_id=right.source_id, present=True, value=right.result),
                    ),
                )
            )

        return tuple(agreements), tuple(differences)


def _comparison_value(source_id: str, value: Any) -> CheckerValue:
    if value is _MISSING:
        return CheckerValue(source_id=source_id, present=False)
    return CheckerValue(source_id=source_id, present=True, value=value)


def _normalize_facts(raw: dict[object, object]) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("worker fact keys must be non-empty strings")
        facts[key] = _json_clone(value, name=f"fact {key}")
    return facts


def _json_clone(value: Any, *, name: str) -> Any:
    _validate_json_value(value, name=name)
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


def _validate_json_value(value: Any, *, name: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        try:
            json.dumps(value, allow_nan=False)
        except ValueError as exc:
            raise ValueError(f"{name} contains a non-finite number") from exc
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, name=name)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{name} object keys must be strings")
            _validate_json_value(item, name=name)
        return
    raise TypeError(f"{name} must be JSON-compatible")


def _require_identifier(value: str, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be text")
    if not value.strip():
        raise ValueError(f"{name} must not be empty")
    if value != value.strip():
        raise ValueError(f"{name} must not contain surrounding whitespace")
