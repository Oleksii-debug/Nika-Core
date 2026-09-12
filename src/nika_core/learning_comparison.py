from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

_MAX_MEMORY_RESULTS = 64
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}\Z")


class ComparisonEvidenceKind(StrEnum):
    EXPERIENCE = "experience"
    MEMORY = "memory"


class MemoryRelation(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    EXTENDS = "extends"
    DUPLICATES = "duplicates"
    UNRELATED = "unrelated"
    UNCERTAIN = "uncertain"


def _canonical_json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest_payload(payload: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_token(value: object, *, field: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{field} must be an exact string")
    if not _TOKEN_RE.fullmatch(value):
        raise ValueError(f"{field} must be a bounded machine token")
    return value


def _require_sha256(value: object, *, field: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{field} must be an exact string")
    if not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field} must be lowercase SHA-256")
    return value


@dataclass(frozen=True, slots=True)
class ComparisonEvidenceRef:
    kind: ComparisonEvidenceKind
    source_namespace_sha256: str
    source_id_sha256: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        if type(self.kind) is not ComparisonEvidenceKind:
            raise TypeError("kind must be a ComparisonEvidenceKind")
        _require_sha256(
            self.source_namespace_sha256,
            field="source_namespace_sha256",
        )
        _require_sha256(self.source_id_sha256, field="source_id_sha256")
        _require_sha256(self.evidence_sha256, field="evidence_sha256")

    def reportable_payload(self) -> dict[str, str]:
        return {
            "evidence_sha256": self.evidence_sha256,
            "kind": self.kind.value,
            "source_id_sha256": self.source_id_sha256,
            "source_namespace_sha256": self.source_namespace_sha256,
        }


@dataclass(frozen=True, slots=True)
class MemoryComparisonResult:
    memory: ComparisonEvidenceRef
    relation: MemoryRelation
    relation_evidence_sha256: str

    def __post_init__(self) -> None:
        if type(self.memory) is not ComparisonEvidenceRef:
            raise TypeError("memory must be a ComparisonEvidenceRef")
        if self.memory.kind is not ComparisonEvidenceKind.MEMORY:
            raise ValueError("memory evidence kind must be MEMORY")
        if type(self.relation) is not MemoryRelation:
            raise TypeError("relation must be a MemoryRelation")
        _require_sha256(
            self.relation_evidence_sha256,
            field="relation_evidence_sha256",
        )

    def reportable_payload(self) -> dict[str, object]:
        return {
            "memory": self.memory.reportable_payload(),
            "relation": self.relation.value,
            "relation_evidence_sha256": self.relation_evidence_sha256,
        }


def _memory_sort_key(result: MemoryComparisonResult) -> tuple[str, str, str]:
    memory = result.memory
    return (
        memory.source_namespace_sha256,
        memory.source_id_sha256,
        memory.evidence_sha256,
    )


def _canonical_memory_results(value: object) -> tuple[MemoryComparisonResult, ...]:
    if type(value) is not tuple:
        raise TypeError("memory_results must be an immutable tuple")
    if not 1 <= len(value) <= _MAX_MEMORY_RESULTS:
        raise ValueError("memory_results count is outside the supported bound")
    if any(type(item) is not MemoryComparisonResult for item in value):
        raise TypeError("memory_results must contain MemoryComparisonResult values")

    ordered = tuple(sorted(value, key=_memory_sort_key))
    logical_ids = tuple(
        (item.memory.source_namespace_sha256, item.memory.source_id_sha256)
        for item in ordered
    )
    if len(set(logical_ids)) != len(logical_ids):
        raise ValueError("duplicate logical memory references are not allowed")
    return ordered


@dataclass(frozen=True, slots=True, init=False)
class ExperienceMemoryComparison:
    comparison_id: str
    workspace_id: str
    agent_id: str
    experience: ComparisonEvidenceRef
    memory_results: tuple[MemoryComparisonResult, ...]
    comparator_sha256: str
    comparison_policy_sha256: str

    @classmethod
    def create(
        cls,
        *,
        comparison_id: str,
        workspace_id: str,
        agent_id: str,
        experience: ComparisonEvidenceRef,
        memory_results: tuple[MemoryComparisonResult, ...],
        comparator_sha256: str,
        comparison_policy_sha256: str,
        expected_comparator_sha256: str,
        expected_comparison_policy_sha256: str,
    ) -> ExperienceMemoryComparison:
        comparison_id = _require_token(comparison_id, field="comparison_id")
        workspace_id = _require_token(workspace_id, field="workspace_id")
        agent_id = _require_token(agent_id, field="agent_id")
        if type(experience) is not ComparisonEvidenceRef:
            raise TypeError("experience must be a ComparisonEvidenceRef")
        if experience.kind is not ComparisonEvidenceKind.EXPERIENCE:
            raise ValueError("experience evidence kind must be EXPERIENCE")
        canonical_results = _canonical_memory_results(memory_results)

        proposed_comparator = _require_sha256(
            comparator_sha256,
            field="comparator_sha256",
        )
        trusted_comparator = _require_sha256(
            expected_comparator_sha256,
            field="expected_comparator_sha256",
        )
        proposed_policy = _require_sha256(
            comparison_policy_sha256,
            field="comparison_policy_sha256",
        )
        trusted_policy = _require_sha256(
            expected_comparison_policy_sha256,
            field="expected_comparison_policy_sha256",
        )
        if proposed_comparator != trusted_comparator:
            raise ValueError("comparator identity does not match trusted authority")
        if proposed_policy != trusted_policy:
            raise ValueError("comparison policy does not match trusted authority")

        instance = object.__new__(cls)
        object.__setattr__(instance, "comparison_id", comparison_id)
        object.__setattr__(instance, "workspace_id", workspace_id)
        object.__setattr__(instance, "agent_id", agent_id)
        object.__setattr__(instance, "experience", experience)
        object.__setattr__(instance, "memory_results", canonical_results)
        object.__setattr__(instance, "comparator_sha256", proposed_comparator)
        object.__setattr__(instance, "comparison_policy_sha256", proposed_policy)
        return instance

    def reportable_payload(self) -> dict[str, Any]:
        return {
            "agent_id_sha256": _digest_text(self.agent_id),
            "comparator_sha256": self.comparator_sha256,
            "comparison_id_sha256": _digest_text(self.comparison_id),
            "comparison_policy_sha256": self.comparison_policy_sha256,
            "experience": self.experience.reportable_payload(),
            "memory_results": [item.reportable_payload() for item in self.memory_results],
            "workspace_id_sha256": _digest_text(self.workspace_id),
        }

    @property
    def comparison_sha256(self) -> str:
        return _digest_payload(self.reportable_payload())
