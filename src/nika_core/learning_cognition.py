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
_MAX_STATEMENT_RAW_CHARS = _MAX_STATEMENT_CHARS * 8
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


def _normalized_statement(value: object) -> str:
    if type(value) is not str:
        raise TypeError("statement must be an exact string")
    if len(value) > _MAX_STATEMENT_RAW_CHARS:
        raise ValueError("statement exceeds the pre-normalization bound")
    normalized = unicodedata.normalize("NFC", value)
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("statement must be valid UTF-8 text") from exc
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


def _require_bounded_tuple(
    value: object,
    *,
    field: str,
    minimum: int,
    maximum: int,
) -> tuple[Any, ...]:
    if type(value) is not tuple:
        raise TypeError(f"{field} must be an immutable tuple")
    if not minimum <= len(value) <= maximum:
        raise ValueError(f"{field} count is outside the supported bound")
    return value


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


def _canonical_evidence_ref(value: object) -> CognitionEvidenceRef:
    if type(value) is not CognitionEvidenceRef:
        raise TypeError("evidence must contain CognitionEvidenceRef values")
    try:
        return CognitionEvidenceRef(
            source_type=value.source_type,
            source_id=value.source_id,
            evidence_sha256=value.evidence_sha256,
        )
    except AttributeError as exc:
        raise TypeError("cognition evidence is missing canonical fields") from exc


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
        _require_bounded_tuple(
            self.evidence,
            field="evidence",
            minimum=1,
            maximum=_MAX_EVIDENCE_REFS,
        )
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
        if cls is not CognitionCandidate:
            raise TypeError("candidate factory must produce the canonical type")
        bounded_evidence = _require_bounded_tuple(
            evidence,
            field="evidence",
            minimum=1,
            maximum=_MAX_EVIDENCE_REFS,
        )
        canonical_evidence = tuple(_canonical_evidence_ref(item) for item in bounded_evidence)
        return cls(
            candidate_id=candidate_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            kind=kind,
            statement=_normalized_statement(statement),
            evidence=tuple(sorted(canonical_evidence, key=_evidence_sort_key)),
        )

    @classmethod
    def revalidate(cls, value: object) -> CognitionCandidate:
        """Return a canonical copy after reconstructing every candidate invariant."""
        if cls is not CognitionCandidate:
            raise TypeError("candidate revalidation must use the canonical type")
        if type(value) is not CognitionCandidate:
            raise TypeError("candidate must be the canonical exact type")
        try:
            return cls.create(
                candidate_id=value.candidate_id,
                workspace_id=value.workspace_id,
                agent_id=value.agent_id,
                kind=value.kind,
                statement=value.statement,
                evidence=value.evidence,
            )
        except AttributeError as exc:
            raise TypeError("candidate is missing canonical fields") from exc

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


def _canonical_requirements(
    value: object,
    *,
    field: str,
) -> tuple[CognitionVerificationRequirement, ...]:
    bounded = _require_bounded_tuple(
        value,
        field=field,
        minimum=1,
        maximum=_MAX_REQUIRED_CHECKS,
    )
    canonical: list[CognitionVerificationRequirement] = []
    for item in bounded:
        if type(item) is not CognitionVerificationRequirement:
            raise TypeError(f"{field} must contain CognitionVerificationRequirement values")
        try:
            canonical.append(
                CognitionVerificationRequirement(
                    check_id=item.check_id,
                    verifier_sha256=item.verifier_sha256,
                )
            )
        except AttributeError as exc:
            raise TypeError(f"{field} contains an incomplete requirement") from exc
    ordered = tuple(sorted(canonical, key=_requirement_sort_key))
    requirement_ids = tuple(item.check_id for item in ordered)
    if len(set(requirement_ids)) != len(requirement_ids):
        raise ValueError("verification requirement ids must be unique")
    return ordered


def _canonical_checks(value: object) -> tuple[CognitionVerificationCheck, ...]:
    bounded = _require_bounded_tuple(
        value,
        field="checks",
        minimum=0,
        maximum=_MAX_REQUIRED_CHECKS,
    )
    canonical: list[CognitionVerificationCheck] = []
    for item in bounded:
        if type(item) is not CognitionVerificationCheck:
            raise TypeError("checks must contain CognitionVerificationCheck values")
        try:
            canonical.append(
                CognitionVerificationCheck(
                    check_id=item.check_id,
                    verifier_sha256=item.verifier_sha256,
                    evidence_sha256=item.evidence_sha256,
                    passed=item.passed,
                )
            )
        except AttributeError as exc:
            raise TypeError("checks contain an incomplete verification check") from exc
    ordered = tuple(sorted(canonical, key=_check_sort_key))
    check_ids = tuple(item.check_id for item in ordered)
    if len(set(check_ids)) != len(check_ids):
        raise ValueError("verification check ids must be unique")
    return ordered


@dataclass(frozen=True, slots=True, init=False)
class CognitionVerification:
    candidate_sha256: str
    verification_policy_sha256: str
    requirements: tuple[CognitionVerificationRequirement, ...]
    checks: tuple[CognitionVerificationCheck, ...]

    @classmethod
    def create(
        cls,
        *,
        candidate: CognitionCandidate,
        verification_policy_sha256: str,
        requirements: tuple[CognitionVerificationRequirement, ...],
        checks: tuple[CognitionVerificationCheck, ...],
        expected_verification_policy_sha256: str,
        expected_requirements: tuple[CognitionVerificationRequirement, ...],
    ) -> CognitionVerification:
        if cls is not CognitionVerification:
            raise TypeError("verification factory must produce the canonical type")
        canonical_candidate = CognitionCandidate.revalidate(candidate)

        proposed_policy_sha256 = _require_sha256(
            verification_policy_sha256,
            field="verification_policy_sha256",
        )
        trusted_policy_sha256 = _require_sha256(
            expected_verification_policy_sha256,
            field="expected_verification_policy_sha256",
        )
        proposed_requirements = _canonical_requirements(
            requirements,
            field="requirements",
        )
        trusted_requirements = _canonical_requirements(
            expected_requirements,
            field="expected_requirements",
        )
        canonical_checks = _canonical_checks(checks)

        if proposed_policy_sha256 != trusted_policy_sha256:
            raise ValueError("verification policy does not match the trusted expectation")
        if proposed_requirements != trusted_requirements:
            raise ValueError("verification requirements do not match the trusted policy")

        requirement_ids = tuple(item.check_id for item in trusted_requirements)
        check_ids = tuple(item.check_id for item in canonical_checks)
        if check_ids != requirement_ids:
            raise ValueError("verification checks must exactly match the required check set")
        for requirement, check in zip(trusted_requirements, canonical_checks, strict=True):
            if check.verifier_sha256 != requirement.verifier_sha256:
                raise ValueError("verification check does not match the required verifier")

        instance = object.__new__(cls)
        object.__setattr__(instance, "candidate_sha256", canonical_candidate.candidate_sha256)
        object.__setattr__(
            instance,
            "verification_policy_sha256",
            trusted_policy_sha256,
        )
        object.__setattr__(instance, "requirements", trusted_requirements)
        object.__setattr__(instance, "checks", canonical_checks)
        return instance

    @classmethod
    def revalidate(
        cls,
        value: object,
        *,
        candidate: CognitionCandidate,
        expected_verification_policy_sha256: str,
        expected_requirements: tuple[CognitionVerificationRequirement, ...],
    ) -> CognitionVerification:
        """Return a canonical trusted receipt after reconstructing received evidence."""
        if cls is not CognitionVerification:
            raise TypeError("verification revalidation must use the canonical type")
        if type(value) is not CognitionVerification:
            raise TypeError("verification must be the canonical exact type")
        try:
            canonical = cls.create(
                candidate=candidate,
                verification_policy_sha256=value.verification_policy_sha256,
                requirements=value.requirements,
                checks=value.checks,
                expected_verification_policy_sha256=expected_verification_policy_sha256,
                expected_requirements=expected_requirements,
            )
            received_candidate_sha256 = _require_sha256(
                value.candidate_sha256,
                field="candidate_sha256",
            )
        except AttributeError as exc:
            raise TypeError("verification is missing canonical fields") from exc
        if canonical.candidate_sha256 != received_candidate_sha256:
            raise ValueError("verification candidate does not match the trusted candidate")
        return canonical

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
