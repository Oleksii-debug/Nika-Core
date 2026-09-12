from __future__ import annotations

import json

import pytest

from nika_core.learning_cognition import (
    CognitionCandidate,
    CognitionCandidateKind,
    CognitionEvidenceRef,
    CognitionVerification,
    CognitionVerificationCheck,
    CognitionVerificationDecision,
    CognitionVerificationRequirement,
)

_POLICY_SHA = "9" * 64


def _evidence(
    source_id: str,
    digest_char: str,
    *,
    source_type: str = "audit_event",
) -> CognitionEvidenceRef:
    return CognitionEvidenceRef(
        source_type=source_type,
        source_id=source_id,
        evidence_sha256=digest_char * 64,
    )


def _candidate(
    *,
    statement: str = "A restart-safe outcome suggests retrying only the missing step.",
    evidence: tuple[CognitionEvidenceRef, ...] | None = None,
    kind: CognitionCandidateKind = CognitionCandidateKind.HYPOTHESIS,
) -> CognitionCandidate:
    selected_evidence = evidence if evidence is not None else (_evidence("event-1", "a"),)
    return CognitionCandidate.create(
        candidate_id="candidate-private-1",
        workspace_id="workspace-private-1",
        agent_id="agent-private-1",
        kind=kind,
        statement=statement,
        evidence=selected_evidence,
    )


def _requirement(check_id: str, verifier_char: str = "f") -> CognitionVerificationRequirement:
    return CognitionVerificationRequirement(
        check_id=check_id,
        verifier_sha256=verifier_char * 64,
    )


def _check(
    check_id: str,
    *,
    passed: bool,
    digest_char: str,
    verifier_char: str = "f",
) -> CognitionVerificationCheck:
    return CognitionVerificationCheck(
        check_id=check_id,
        verifier_sha256=verifier_char * 64,
        evidence_sha256=digest_char * 64,
        passed=passed,
    )


def test_candidate_identity_is_stable_for_equivalent_evidence_order_and_unicode() -> None:
    first = _evidence("event-1", "a")
    second = _evidence("event-2", "b", source_type="memory")
    decomposed = "Cafe\N{COMBINING ACUTE ACCENT} evidence supports the hypothesis."
    composed = "Caf\N{LATIN SMALL LETTER E WITH ACUTE} evidence supports the hypothesis."

    candidate_a = _candidate(statement=decomposed, evidence=(second, first))
    candidate_b = _candidate(statement=composed, evidence=(first, second))

    assert candidate_a.statement == composed
    assert candidate_a.candidate_sha256 == candidate_b.candidate_sha256
    assert candidate_a.evidence == (first, second)


def test_candidate_reportable_evidence_minimizes_raw_text_and_identifiers() -> None:
    secret_text = "transient hypothesis with private canary SECRET-CANARY-42"
    candidate = _candidate(statement=secret_text)

    serialized = json.dumps(candidate.reportable_payload(), ensure_ascii=False, sort_keys=True)

    assert secret_text not in serialized
    assert "SECRET-CANARY-42" not in serialized
    assert candidate.candidate_id not in serialized
    assert candidate.workspace_id not in serialized
    assert candidate.agent_id not in serialized
    assert candidate.evidence[0].source_id not in serialized
    assert candidate.statement_sha256 in serialized


def test_candidate_identity_changes_when_bound_evidence_changes() -> None:
    candidate_a = _candidate(evidence=(_evidence("event-1", "a"),))
    candidate_b = _candidate(evidence=(_evidence("event-1", "b"),))

    assert candidate_a.candidate_sha256 != candidate_b.candidate_sha256


def test_abstraction_requires_multiple_evidence_references() -> None:
    with pytest.raises(ValueError, match="at least two evidence references"):
        _candidate(kind=CognitionCandidateKind.ABSTRACTION)


def test_abstraction_with_multiple_bound_evidence_is_valid() -> None:
    candidate = _candidate(
        kind=CognitionCandidateKind.ABSTRACTION,
        evidence=(
            _evidence("event-2", "b"),
            _evidence("event-1", "a"),
        ),
    )

    assert candidate.kind is CognitionCandidateKind.ABSTRACTION
    assert len(candidate.evidence) == 2


def test_duplicate_evidence_reference_is_rejected() -> None:
    evidence = _evidence("event-1", "a")

    with pytest.raises(ValueError, match="duplicate evidence"):
        _candidate(evidence=(evidence, evidence))


def test_statement_bounds_and_control_characters_fail_closed() -> None:
    with pytest.raises(ValueError, match="supported bound"):
        _candidate(statement="x" * 16_385)
    with pytest.raises(ValueError, match="control characters"):
        _candidate(statement="unsafe\x00statement")


def test_verification_is_exact_set_bound_and_order_independent_at_creation() -> None:
    candidate = _candidate()
    requirement_a = _requirement("causality")
    requirement_b = _requirement("replay")
    check_a = _check("causality", passed=True, digest_char="1")
    check_b = _check("replay", passed=True, digest_char="2")

    verification_a = CognitionVerification.create(
        candidate=candidate,
        verification_policy_sha256=_POLICY_SHA,
        requirements=(requirement_b, requirement_a),
        checks=(check_b, check_a),
    )
    verification_b = CognitionVerification.create(
        candidate=candidate,
        verification_policy_sha256=_POLICY_SHA,
        requirements=(requirement_a, requirement_b),
        checks=(check_a, check_b),
    )

    assert verification_a.decision is CognitionVerificationDecision.VERIFIED
    assert verification_a.required_check_ids == ("causality", "replay")
    assert verification_a.verification_sha256 == verification_b.verification_sha256


def test_failed_required_check_rejects_candidate_without_losing_evidence() -> None:
    candidate = _candidate()
    verification = CognitionVerification.create(
        candidate=candidate,
        verification_policy_sha256=_POLICY_SHA,
        requirements=(
            _requirement("causality"),
            _requirement("replay"),
        ),
        checks=(
            _check("causality", passed=True, digest_char="1"),
            _check("replay", passed=False, digest_char="2"),
        ),
    )

    assert verification.decision is CognitionVerificationDecision.REJECTED
    assert verification.reportable_payload()["candidate_sha256"] == candidate.candidate_sha256
    assert verification.verification_sha256


def test_missing_or_extra_verification_check_fails_closed() -> None:
    candidate = _candidate()
    requirement = _requirement("causality")
    check = _check("causality", passed=True, digest_char="1")

    with pytest.raises(ValueError, match="exactly match"):
        CognitionVerification.create(
            candidate=candidate,
            verification_policy_sha256=_POLICY_SHA,
            requirements=(requirement, _requirement("replay")),
            checks=(check,),
        )
    with pytest.raises(ValueError, match="exactly match"):
        CognitionVerification.create(
            candidate=candidate,
            verification_policy_sha256=_POLICY_SHA,
            requirements=(requirement,),
            checks=(
                check,
                _check("replay", passed=True, digest_char="2"),
            ),
        )


def test_substituted_verifier_fails_closed_even_under_same_check_id() -> None:
    candidate = _candidate()

    with pytest.raises(ValueError, match="required verifier"):
        CognitionVerification.create(
            candidate=candidate,
            verification_policy_sha256=_POLICY_SHA,
            requirements=(_requirement("causality", verifier_char="a"),),
            checks=(
                _check(
                    "causality",
                    passed=True,
                    digest_char="1",
                    verifier_char="b",
                ),
            ),
        )


def test_duplicate_requirement_id_is_rejected() -> None:
    candidate = _candidate()
    requirement = _requirement("causality")
    check = _check("causality", passed=True, digest_char="1")

    with pytest.raises(ValueError, match="requirement ids must be unique"):
        CognitionVerification(
            candidate_sha256=candidate.candidate_sha256,
            verification_policy_sha256=_POLICY_SHA,
            requirements=(requirement, requirement),
            checks=(check,),
        )


def test_verification_check_requires_real_bool_and_strict_hashes() -> None:
    with pytest.raises(TypeError, match="passed must be a bool"):
        CognitionVerificationCheck(
            check_id="causality",
            verifier_sha256="a" * 64,
            evidence_sha256="b" * 64,
            passed=1,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        CognitionEvidenceRef(
            source_type="audit_event",
            source_id="event-1",
            evidence_sha256="A" * 64,
        )
