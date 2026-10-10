from __future__ import annotations

import hashlib
import hmac
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass

from nika_core.research.models import (
    FreshnessState,
    ResearchEvidence,
    ResearchResultItem,
    ResearchResultSet,
    SourceKind,
    SourceSpec,
)

_ASSIGNMENT_SCHEMA = "nika.multi_agent.source-inspection-assignment:v2"
_RESULT_SCHEMA = "nika.multi_agent.source-inspection-result:v2"
_MAX_RESULT_JSON_BYTES = 1_048_576
_MAX_RESULT_JSON_NODES = 10_000
_MAX_RESULT_JSON_DEPTH = 32
_MAX_RESULT_INTEGER_BITS = 4_096


class SourceResultBindingError(ValueError):
    """Raised when worker-controlled evidence fails its trusted assignment binding."""


@dataclass(frozen=True, slots=True)
class SourceInspectionAssignment:
    """One durable source task, bound independently to team and logical task identity."""

    team_id: str
    task_id: str
    assignment_id: str
    member_id: str
    source: SourceSpec
    tool_call_id: str
    effect_id: str
    max_items: int

    def __post_init__(self) -> None:
        if not isinstance(self.source, SourceSpec):
            raise TypeError("source must be a SourceSpec")
        if not isinstance(self.source.kind, SourceKind):
            raise TypeError("source kind must be a SourceKind")
        for label, value in (
            ("team_id", self.team_id),
            ("task_id", self.task_id),
            ("assignment_id", self.assignment_id),
            ("member_id", self.member_id),
            ("source_id", self.source.source_id),
            ("workspace_id", self.source.workspace_id),
            ("locator", self.source.locator),
            ("tool_call_id", self.tool_call_id),
            ("effect_id", self.effect_id),
        ):
            _require_text(value, label)
        if isinstance(self.max_items, bool) or not isinstance(self.max_items, int):
            raise TypeError("max_items must be an integer")
        if not 1 <= self.max_items <= 100:
            raise ValueError("max_items must be between 1 and 100")

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": _ASSIGNMENT_SCHEMA,
            "team_id": self.team_id,
            "task_id": self.task_id,
            "assignment_id": self.assignment_id,
            "member_id": self.member_id,
            "source": {
                "source_id": self.source.source_id,
                "workspace_id": self.source.workspace_id,
                "source_kind": self.source.kind.value,
                "locator": self.source.locator,
            },
            "tool_call_id": self.tool_call_id,
            "effect_id": self.effect_id,
            "max_items": self.max_items,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> SourceInspectionAssignment:
        data = _mapping(payload, "assignment")
        _require_exact_keys(
            data,
            {
                "schema",
                "team_id",
                "task_id",
                "assignment_id",
                "member_id",
                "source",
                "tool_call_id",
                "effect_id",
                "max_items",
            },
            "assignment",
        )
        if data["schema"] != _ASSIGNMENT_SCHEMA:
            raise SourceResultBindingError("unsupported source inspection assignment schema")
        source_data = _mapping(data["source"], "assignment source")
        _require_exact_keys(
            source_data,
            {"source_id", "workspace_id", "source_kind", "locator"},
            "assignment source",
        )
        try:
            source_kind = SourceKind(_text(source_data["source_kind"], "source_kind"))
            return cls(
                team_id=_text(data["team_id"], "team_id"),
                task_id=_text(data["task_id"], "task_id"),
                assignment_id=_text(data["assignment_id"], "assignment_id"),
                member_id=_text(data["member_id"], "member_id"),
                source=SourceSpec(
                    source_id=_text(source_data["source_id"], "source_id"),
                    workspace_id=_text(source_data["workspace_id"], "workspace_id"),
                    kind=source_kind,
                    locator=_text(source_data["locator"], "locator"),
                ),
                tool_call_id=_text(data["tool_call_id"], "tool_call_id"),
                effect_id=_text(data["effect_id"], "effect_id"),
                max_items=_integer(data["max_items"], "max_items"),
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, SourceResultBindingError):
                raise
            raise SourceResultBindingError(str(exc)) from exc


def encode_source_result(
    assignment: SourceInspectionAssignment,
    result_set: ResearchResultSet,
) -> dict[str, object]:
    """Encode canonical Research evidence with a stable context-and-content digest."""
    _validate_result_set(assignment, result_set)
    unsigned = _unsigned_result_payload(assignment, _encode_result_set(result_set))
    return {**unsigned, "result_digest": _payload_digest(unsigned)}


def decode_source_result(
    assignment: SourceInspectionAssignment,
    *,
    member_id: str,
    output: Mapping[str, object],
) -> ResearchResultSet:
    """Validate worker bytes against every trusted identity before decoding evidence."""
    trusted_member = _text(member_id, "trusted member_id")
    if trusted_member != assignment.member_id:
        raise SourceResultBindingError("worker member identity does not match assignment")

    data = _mapping(output, "source inspection result")
    _require_exact_keys(
        data,
        {
            "schema",
            "team_id",
            "task_id",
            "assignment_id",
            "member_id",
            "source_id",
            "source_kind",
            "workspace_id",
            "locator",
            "tool_call_id",
            "effect_id",
            "result_set",
            "result_digest",
        },
        "source inspection result",
    )
    if data["schema"] != _RESULT_SCHEMA:
        raise SourceResultBindingError("unsupported source inspection result schema")

    expected = {
        "team_id": assignment.team_id,
        "task_id": assignment.task_id,
        "assignment_id": assignment.assignment_id,
        "member_id": assignment.member_id,
        "source_id": assignment.source.source_id,
        "source_kind": assignment.source.kind.value,
        "workspace_id": assignment.source.workspace_id,
        "locator": assignment.source.locator,
        "tool_call_id": assignment.tool_call_id,
        "effect_id": assignment.effect_id,
    }
    for key, expected_value in expected.items():
        actual = _text(data[key], key)
        if actual != expected_value:
            raise SourceResultBindingError(f"{key} does not match declared assignment")

    # Check the item count before hashing worker-controlled evidence. A forged
    # result cannot demand unbounded serialization work to reach this limit.
    raw_result = _mapping(data["result_set"], "result_set")
    _require_exact_keys(
        raw_result, {"result_set_id", "workspace_id", "query", "created_at", "items"}, "result_set"
    )
    raw_items = raw_result["items"]
    if not isinstance(raw_items, list):
        raise SourceResultBindingError("result_set items must be a list")
    if len(raw_items) > assignment.max_items:
        raise SourceResultBindingError("result exceeds assignment max_items")

    claimed_digest = _text(data["result_digest"], "result_digest")
    if len(claimed_digest) != 64 or any(ch not in "0123456789abcdef" for ch in claimed_digest):
        raise SourceResultBindingError("invalid result_digest")
    unsigned = {key: data[key] for key in data if key != "result_digest"}
    actual_digest = _payload_digest(unsigned)
    if not hmac.compare_digest(claimed_digest, actual_digest):
        raise SourceResultBindingError("result_digest does not match canonical result evidence")

    result_set = _decode_result_set(data["result_set"])
    _validate_result_set(assignment, result_set)
    return result_set


def _unsigned_result_payload(
    assignment: SourceInspectionAssignment,
    result_set: dict[str, object],
) -> dict[str, object]:
    return {
        "schema": _RESULT_SCHEMA,
        "team_id": assignment.team_id,
        "task_id": assignment.task_id,
        "assignment_id": assignment.assignment_id,
        "member_id": assignment.member_id,
        "source_id": assignment.source.source_id,
        "source_kind": assignment.source.kind.value,
        "workspace_id": assignment.source.workspace_id,
        "locator": assignment.source.locator,
        "tool_call_id": assignment.tool_call_id,
        "effect_id": assignment.effect_id,
        "result_set": result_set,
    }


def _payload_digest(payload: Mapping[str, object]) -> str:
    _validate_json_budget(payload)
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError, OverflowError) as exc:
        raise SourceResultBindingError("result evidence must be canonical JSON") from exc
    if len(encoded) > _MAX_RESULT_JSON_BYTES:
        raise SourceResultBindingError("result evidence exceeds bounded JSON admission")
    return hashlib.sha256(encoded).hexdigest()


def _validate_json_budget(
    value: object,
    *,
    active_containers: set[int] | None = None,
    budget: dict[str, int] | None = None,
    depth: int = 0,
) -> None:
    budget = budget if budget is not None else {"nodes": 0, "text_bytes": 0}
    budget["nodes"] += 1
    if budget["nodes"] > _MAX_RESULT_JSON_NODES or depth > _MAX_RESULT_JSON_DEPTH:
        raise SourceResultBindingError("result evidence exceeds bounded JSON admission")

    value_type = type(value)
    if value is None or value_type is bool:
        return
    if value_type is int:
        if value.bit_length() > _MAX_RESULT_INTEGER_BITS:
            raise SourceResultBindingError("result evidence exceeds bounded JSON admission")
        return
    if value_type is float:
        if not math.isfinite(value):
            raise SourceResultBindingError("result evidence must be canonical JSON")
        return
    if value_type is str:
        _account_json_text(value, budget)
        return
    if value_type not in {list, dict}:
        raise SourceResultBindingError("result evidence must be canonical JSON")

    active = active_containers if active_containers is not None else set()
    identity = id(value)
    if identity in active:
        raise SourceResultBindingError("result evidence must be canonical JSON")
    active.add(identity)
    try:
        if value_type is list:
            for item in value:
                _validate_json_budget(
                    item,
                    active_containers=active,
                    budget=budget,
                    depth=depth + 1,
                )
            return
        for key, item in value.items():
            if type(key) is not str:
                raise SourceResultBindingError("result evidence must be canonical JSON")
            _account_json_text(key, budget)
            _validate_json_budget(
                item,
                active_containers=active,
                budget=budget,
                depth=depth + 1,
            )
    except RecursionError as exc:
        raise SourceResultBindingError("result evidence exceeds bounded JSON admission") from exc
    finally:
        active.remove(identity)


def _account_json_text(value: str, budget: dict[str, int]) -> None:
    if len(value) > _MAX_RESULT_JSON_BYTES:
        raise SourceResultBindingError("result evidence exceeds bounded JSON admission")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise SourceResultBindingError("result evidence must be valid UTF-8 JSON") from exc
    budget["text_bytes"] += len(encoded)
    if budget["text_bytes"] > _MAX_RESULT_JSON_BYTES:
        raise SourceResultBindingError("result evidence exceeds bounded JSON admission")


def _validate_result_set(
    assignment: SourceInspectionAssignment,
    result_set: ResearchResultSet,
) -> None:
    if not isinstance(result_set, ResearchResultSet):
        raise TypeError("result_set must be a ResearchResultSet")
    for label, value in (
        ("result_set_id", result_set.result_set_id),
        ("workspace_id", result_set.workspace_id),
        ("query", result_set.query),
        ("created_at", result_set.created_at),
    ):
        _require_text(value, label)
    if result_set.workspace_id != assignment.source.workspace_id:
        raise SourceResultBindingError("result workspace does not match declared source")
    if len(result_set.items) > assignment.max_items:
        raise SourceResultBindingError("result exceeds assignment max_items")

    ordinals: set[int] = set()
    document_ids: set[str] = set()
    evidence_count = 0
    for item in result_set.items:
        if not isinstance(item, ResearchResultItem):
            raise TypeError("result items must be ResearchResultItem values")
        if isinstance(item.ordinal, bool) or not isinstance(item.ordinal, int):
            raise SourceResultBindingError("result ordinal must be an integer")
        if item.ordinal < 0 or item.ordinal in ordinals:
            raise SourceResultBindingError("result ordinal is invalid or duplicated")
        ordinals.add(item.ordinal)
        _require_text(item.document_id, "document_id")
        if item.document_id in document_ids:
            raise SourceResultBindingError("result document_id is duplicated")
        document_ids.add(item.document_id)
        _require_text(item.title, "title")
        _string(item.snippet, "snippet")
        _require_text(item.why_matched, "why_matched")
        _finite_rank(item.rank)
        if not item.evidence:
            raise SourceResultBindingError("result item has no provenance evidence")
        try:
            evidence_count += len(item.evidence)
        except TypeError as exc:
            raise SourceResultBindingError("result evidence must be a bounded sequence") from exc
        if evidence_count > _MAX_RESULT_JSON_NODES:
            raise SourceResultBindingError("result evidence exceeds bounded JSON admission")
        for evidence in item.evidence:
            _validate_evidence(assignment.source, evidence)


def _validate_evidence(source: SourceSpec, evidence: ResearchEvidence) -> None:
    if not isinstance(evidence, ResearchEvidence):
        raise TypeError("result evidence must be ResearchEvidence")
    if evidence.source_id != source.source_id:
        raise SourceResultBindingError("evidence source_id does not match declared source")
    if evidence.source_kind is not source.kind:
        raise SourceResultBindingError("evidence source_kind does not match declared source")
    if evidence.locator != source.locator:
        raise SourceResultBindingError("evidence locator does not match declared source")
    _require_text(evidence.observed_at, "observed_at")
    if evidence.freshness is not None and not isinstance(evidence.freshness, FreshnessState):
        raise SourceResultBindingError("evidence freshness is invalid")


def _encode_result_set(result_set: ResearchResultSet) -> dict[str, object]:
    return {
        "result_set_id": result_set.result_set_id,
        "workspace_id": result_set.workspace_id,
        "query": result_set.query,
        "created_at": result_set.created_at,
        "items": [
            {
                "ordinal": item.ordinal,
                "document_id": item.document_id,
                "title": item.title,
                "snippet": item.snippet,
                "rank": item.rank,
                "why_matched": item.why_matched,
                "evidence": [
                    {
                        "source_id": evidence.source_id,
                        "source_kind": evidence.source_kind.value,
                        "locator": evidence.locator,
                        "observed_at": evidence.observed_at,
                        "freshness": (
                            evidence.freshness.value if evidence.freshness is not None else None
                        ),
                    }
                    for evidence in item.evidence
                ],
            }
            for item in result_set.items
        ],
    }


def _decode_result_set(value: object) -> ResearchResultSet:
    data = _mapping(value, "result_set")
    _require_exact_keys(
        data,
        {"result_set_id", "workspace_id", "query", "created_at", "items"},
        "result_set",
    )
    raw_items = data["items"]
    if not isinstance(raw_items, list):
        raise SourceResultBindingError("result_set items must be a list")
    return ResearchResultSet(
        result_set_id=_text(data["result_set_id"], "result_set_id"),
        workspace_id=_text(data["workspace_id"], "workspace_id"),
        query=_text(data["query"], "query"),
        created_at=_text(data["created_at"], "created_at"),
        items=tuple(_decode_item(item) for item in raw_items),
    )


def _decode_item(value: object) -> ResearchResultItem:
    data = _mapping(value, "result item")
    _require_exact_keys(
        data,
        {"ordinal", "document_id", "title", "snippet", "rank", "why_matched", "evidence"},
        "result item",
    )
    raw_evidence = data["evidence"]
    if not isinstance(raw_evidence, list):
        raise SourceResultBindingError("result evidence must be a list")
    rank = _finite_rank(data["rank"])
    return ResearchResultItem(
        ordinal=_integer(data["ordinal"], "ordinal"),
        document_id=_text(data["document_id"], "document_id"),
        title=_text(data["title"], "title"),
        snippet=_string(data["snippet"], "snippet"),
        rank=rank,
        why_matched=_text(data["why_matched"], "why_matched"),
        evidence=tuple(_decode_evidence(item) for item in raw_evidence),
    )


def _finite_rank(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SourceResultBindingError("result rank must be numeric")
    try:
        rank = float(value)
    except OverflowError as exc:
        raise SourceResultBindingError("result rank must be finite") from exc
    if not math.isfinite(rank):
        raise SourceResultBindingError("result rank must be finite")
    return rank


def _decode_evidence(value: object) -> ResearchEvidence:
    data = _mapping(value, "research evidence")
    _require_exact_keys(
        data,
        {"source_id", "source_kind", "locator", "observed_at", "freshness"},
        "research evidence",
    )
    try:
        source_kind = SourceKind(_text(data["source_kind"], "source_kind"))
        freshness_value = data["freshness"]
        freshness = (
            None
            if freshness_value is None
            else FreshnessState(_text(freshness_value, "freshness"))
        )
    except ValueError as exc:
        raise SourceResultBindingError("invalid research evidence enum") from exc
    return ResearchEvidence(
        source_id=_text(data["source_id"], "source_id"),
        source_kind=source_kind,
        locator=_text(data["locator"], "locator"),
        observed_at=_text(data["observed_at"], "observed_at"),
        freshness=freshness,
    )


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise SourceResultBindingError(f"{label} must be an object")
    return value


def _require_exact_keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise SourceResultBindingError(f"{label} fields do not match schema")


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SourceResultBindingError(f"{label} must be an integer")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise SourceResultBindingError(f"{label} must be text")
    if len(value) > _MAX_RESULT_JSON_BYTES:
        raise SourceResultBindingError("result evidence exceeds bounded JSON admission")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise SourceResultBindingError(f"{label} must be valid UTF-8 text") from exc
    if len(encoded) > _MAX_RESULT_JSON_BYTES:
        raise SourceResultBindingError("result evidence exceeds bounded JSON admission")
    return value


def _text(value: object, label: str) -> str:
    text = _string(value, label)
    _require_canonical_text(text, label)
    return text


def _require_text(value: object, label: str) -> None:
    text = _string(value, label)
    _require_canonical_text(text, label)


def _require_canonical_text(value: str, label: str) -> None:
    if not value or not value.strip():
        raise SourceResultBindingError(f"{label} must be non-empty text")
    if value != value.strip():
        raise SourceResultBindingError(f"{label} must not contain surrounding whitespace")
