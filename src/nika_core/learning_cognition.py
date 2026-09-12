from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

_MAX_EVIDENCE_REFS = 64
_MAX_REQUIRED_CHECKS = 32
_MAX_STATEMENT_CHARS = 16_384
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}\Z")


class CognitionCandidateKind(StrEnum):
    HYPOTHESIS = "hypothesis"
    ABSTRACTION = "abstraction"


class CognitionVerificationDecision(StrEnum):
    VERIFIED = "verified"
    REJECTED = "rejected"


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
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    if not _TOKEN_RE.fullmatch(value):
        raise ValueError(f"{field} must be a bounded machine token")
    return value


def _require_sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    if not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field} must be lowercase SHA-256")
    return value


def _normalized_statement(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("statement must be a string")
    normalized = unicodedata.normalize("NFC", value)
    if not normalized.strip():
        raise ValueError("statement must not be empty")
    if len(normalized) > _MAX_STATEMENT_CHARS:
        raise ValueError("statement exceeds the supported bound")
    if any(ord(char) < 32 and char not in {"\n", "\t"} for char in normalized):
        raise ValueError("statement contains unsupported control characters")
    return normalized


def _evidence_sort_key(value: CognitionEvidenceRef) -> tuple[str, str, str]:
    return (value.source_type, value.source_id, value.evidence_sha256)


def _requirement_sort_key(value: CognitionVerificationRequirement) -> str:
    return value.check_id


def _check_sort_key(value: CognitionVerificationCheck) -> str:
    return value.check_id


@dataclass(frozen=True, slots=True)
class CognitionEvidenceRef:
    source_type: str
    source_id: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        _require_token(self.source_type, field="source_type")
        _require_token(self.source_id, field="source_id")
        _require_sha256(self.evidence_sha256, field="evidence_sha256")

    def canonical_payload(self) -> dict[str, str]:
        return {
            "evidence_sha256": self.evidence_sha256,
            "source_id_sha256": _digest_text(self.source_id),
            "source_type": self.source_type,
        }


@dataclass(frozen=True, slots=True)
class CognitionCandidate:
    candidate_id: str
    workspace_id: str
    agent_id: str
    kind: CognitionCandidateKind
    statement: str
    evidence: tuple[CognitionEvidenceRef, ...]

    def __post_init__(self) -> None:
        _require_token(self.candidate_id, field="candidate_id")
        _require_token(self.workspace_id, field="workspace_id")
        _require_token(self.agent_id, field="agent_id")
        if type(self.kind) is not CognitionCandidateKind:
            raise TypeError("kind must be a CognitionCandidateKind")
        if self.statement != _normalized_statement(self.statement):
            raise ValueError("statement must use NFC normalization")
        if type(self.evidence) is not tuple:
            raise TypeError("evidence must be an immutable tuple")
        if not 1 <= len(self.evidence) <= _MAX_EVIDENCE_REFS:
            raise ValueError("evidence count is outside the supported bound")
        if any(type(item) is not CognitionEvidenceRef for item in self.evidence):
            raise TypeError("evidence must contain CognitionEvidenceRef values")
        if self.evidence != tuple(sorted(self.evidence, key=_evidence_sort_key)):
            raise ValueError("evidence order is not canonical")
        identities = [
            (item.source_type, item.source_id, item.evidence_sha256) for item in self.evidence
        ]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate evidence references are not allowed")
        if self.kind is CognitionCandidateKind.ABSTRACTION and len(self.evidence) < 2:
            raise ValueError("an abstraction requires at least two evidence references")

    @classmethod
    def create(
        cls,
        *,
        candidate_id: str,
        workspace_id: str,
        agent_id: str,
        kind: CognitionCandidateKind,
        statement: str,
        evidence: tuple[CognitionEvidenceRef, ...],
    ) -> CognitionCandidate:
        if type(evidence) is not tuple:
            raise TypeError("evidence must be an immutable tuple")
        return cls(
            candidate_id=candidate_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            kind=kind,
            statement=_normalized_statement(statement),
            evidence=tuple(sorted(evidence, key=_evidence_sort_key)),
        )

    @property
    def statement_sha256(self) -> str:
        return _digest_text(self.statement)

    def reportable_payload(self) -> dict[str, Any]:
        return {
            "agent_id_sha256": _digest_text(self.agent_id),
            "candidate_id_sha256": _digest_text(self.candidate_id),
            "evidence": [item.canonical_payload() for item in self.evidence],
            "kind": self.kind.value,
            "statement_char_count": len(self.statement),
            "statement_sha256": self.statement_sha256,
            "workspace_id_sha256": _digest_text(self.workspace_id),
        }

    @property
    def candidate_sha256(self) -> str:
        return _digest_payload(self.reportable_payload())


@dataclass(frozen=True, slots=True)
class CognitionVerificationRequirement:
    check_id: str
    verifier_sha256: str

    def __post_init__(self) -> None:
        _require_token(self.check_id, field="check_id")
        _require_sha256(self.verifier_sha256, field="verifier_sha256")

    def canonical_payload(self) -> dict[str, str]:
        return {
            "check_id": self.check_id,
            "verifier_sha256": self.verifier_sha256,
        }


@dataclass(frozen=True, slots=True)
class CognitionVerificationCheck:
    check_id: str
    verifier_sha256: str
    evidence_sha256: str
    passed: bool

    def __post_init__(self) -> None:
        _require_token(self.check_id, field="check_id")
        _require_sha256(self.verifier_sha256, field="verifier_sha256")
        _require_sha256(self.evidence_sha256, field="evidence_sha256")
        if type(self.passed) is not bool:
            raise TypeError("passed must be a bool")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "check_id": self.check_id,
            "evidence_sha256": self.evidence_sha256,
            "passed": self.passed,
            "verifier_sha256": self.verifier_sha256,
        }


@dataclass(frozen=True, slots=True)
class CognitionVerification:
    candidate_sha256: str
    verification_policy_sha256: str
    requirements: tuple[CognitionVerificationRequirement, ...]
    checks: tuple[CognitionVerificationCheck, ...]

    def __post_init__(self) -> None:
        _require_sha256(self.candidate_sha256, field="candidate_sha256")
        _require_sha256(self.verification_policy_sha256, field="verification_policy_sha256")
        if type(self.requirements) is not tuple:
            raise TypeError("requirements must be an immutable tuple")
        if not 1 <= len(self.requirements) <= _MAX_REQUIRED_CHECKS:
            raise ValueError("requirement count is outside the supported bound")
        if any(type(item) is not CognitionVerificationRequirement for item in self.requirements):
            raise TypeError("requirements must contain CognitionVerificationRequirement values")
        if self.requirements != tuple(sorted(self.requirements, key=_requirement_sort_key)):
            raise ValueError("verification requirements are not in canonical order")
        requirement_ids = tuple(item.check_id for item in self.requirements)
        if len(set(requirement_ids)) != len(requirement_ids):
            raise ValueError("verification requirement ids must be unique")
        if type(self.checks) is not tuple:
            raise TypeError("checks must be an immutable tuple")
        if any(type(item) is not CognitionVerificationCheck for item in self.checks):
            raise TypeError("checks must contain CognitionVerificationCheck values")
        if self.checks != tuple(sorted(self.checks, key=_check_sort_key)):
            raise ValueError("verification checks are not in canonical order")
        check_ids = tuple(item.check_id for item in self.checks)
        if len(set(check_ids)) != len(check_ids):
            raise ValueError("verification check ids must be unique")
        if check_ids != requirement_ids:
            raise ValueError("verification checks must exactly match the required check set")
        for requirement, check in zip(self.requirements, self.checks, strict=True):
            if check.verifier_sha256 != requirement.verifier_sha256:
                raise ValueError("verification check does not match the required verifier")

    @classmethod
    def create(
        cls,
        *,
        candidate: CognitionCandidate,
        verification_policy_sha256: str,
        requirements: tuple[CognitionVerificationRequirement, ...],
        checks: tuple[CognitionVerificationCheck, ...],
    ) -> CognitionVerification:
        if type(candidate) is not CognitionCandidate:
            raise TypeError("candidate must be a CognitionCandidate")
        if type(requirements) is not tuple:
            raise TypeError("requirements must be an immutable tuple")
        if type(checks) is not tuple:
            raise TypeError("checks must be an immutable tuple")
        return cls(
            candidate_sha256=candidate.candidate_sha256,
            verification_policy_sha256=verification_policy_sha256,
            requirements=tuple(sorted(requirements, key=_requirement_sort_key)),
            checks=tuple(sorted(checks, key=_check_sort_key)),
        )

    @property
    def required_check_ids(self) -> tuple[str, ...]:
        return tuple(item.check_id for item in self.requirements)

    @property
    def decision(self) -> CognitionVerificationDecision:
        if all(check.passed for check in self.checks):
            return CognitionVerificationDecision.VERIFIED
        return CognitionVerificationDecision.REJECTED

    def reportable_payload(self) -> dict[str, object]:
        return {
            "candidate_sha256": self.candidate_sha256,
            "checks": [check.canonical_payload() for check in self.checks],
            "decision": self.decision.value,
            "requirements": [item.canonical_payload() for item in self.requirements],
            "verification_policy_sha256": self.verification_policy_sha256,
        }

    @property
    def verification_sha256(self) -> str:
        return _digest_payload(self.reportable_payload())
