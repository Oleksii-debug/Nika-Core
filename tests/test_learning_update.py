from dataclasses import FrozenInstanceError

import pytest

from nika_core.learning_cognition import (
    CognitionCandidate,
    CognitionCandidateKind,
    CognitionEvidenceRef,
    CognitionVerification,
    CognitionVerificationCheck,
    CognitionVerificationRequirement,
)
from nika_core.learning_update import (
    LearningUpdateEvidence,
    LearningUpdateIntent,
    LearningUpdateTarget,
)

A = "a" * 64
B = "b" * 64
C = "c" * 64
D = "d" * 64
E = "e" * 64

POLICY = A
VERIFIER = B
SOURCE_EVIDENCE = C
TARGET_REF = D
REVISION = E


def requirements() -> tuple[CognitionVerificationRequirement, ...]:
    return (
        CognitionVerificationRequirement(
            check_id="semantic-truth",
            verifier_sha256=VERIFIER,
        ),
    )


def make_candidate(
    *,
    candidate_id: str = "candidate-001",
    workspace_id: str = "workspace-1",
    agent_id: str = "agent-1",
    statement: str = "bounded verified cognition",
) -> CognitionCandidate:
    return CognitionCandidate.create(
        candidate_id=candidate_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
        kind=CognitionCandidateKind.HYPOTHESIS,
        statement=statement,
        evidence=(
            CognitionEvidenceRef(
                source_type="comparison",
                source_id="comparison-1",
                evidence_sha256=SOURCE_EVIDENCE,
            ),
        ),
    )


def make_verification(
    candidate: CognitionCandidate,
    *,
    passed: bool = True,
    policy: str = POLICY,
    trusted_policy: str = POLICY,
    trusted_requirements: tuple[CognitionVerificationRequirement, ...] | None = None,
) -> CognitionVerification:
    reqs = requirements()
    trusted = reqs if trusted_requirements is None else trusted_requirements
    return CognitionVerification.create(
        candidate=candidate,
        verification_policy_sha256=policy,
        requirements=reqs,
        checks=(
            CognitionVerificationCheck(
                check_id="semantic-truth",
                verifier_sha256=VERIFIER,
                evidence_sha256=A,
                passed=passed,
            ),
        ),
        expected_verification_policy_sha256=trusted_policy,
        expected_requirements=trusted,
    )


def make_intent(
    *,
    candidate: CognitionCandidate | None = None,
    verification: CognitionVerification | None = None,
    target: LearningUpdateTarget = LearningUpdateTarget.MEMORY,
    payload: bytes = b'{"fact":"bounded"}',
    expected_revision_sha256: str | None = None,
) -> LearningUpdateIntent:
    canonical_candidate = candidate or make_candidate()
    canonical_verification = verification or make_verification(canonical_candidate)
    return LearningUpdateIntent.bind_payload(
        intent_id="update-001",
        candidate=canonical_candidate,
        verification=canonical_verification,
        expected_verification_policy_sha256=POLICY,
        expected_requirements=requirements(),
        target=target,
        target_ref_sha256=TARGET_REF,
        update_schema="nika.semantic-update/v1",
        payload=payload,
        expected_revision_sha256=expected_revision_sha256,
    )


@pytest.mark.parametrize("target", list(LearningUpdateTarget))
def test_all_loop_b_targets_are_bound_without_mutation_authority(target):
    intent = make_intent(target=target)

    assert intent.target is target
    assert not hasattr(intent, "apply")
    assert not hasattr(intent, "commit")


def test_factory_derives_scope_and_receipt_identity_from_verified_cognition():
    candidate = make_candidate(workspace_id="workspace-a", agent_id="agent-a")
    verification = make_verification(candidate)

    intent = make_intent(candidate=candidate, verification=verification)

    assert intent.workspace_id == "workspace-a"
    assert intent.agent_id == "agent-a"
    assert intent.candidate_sha256 == candidate.candidate_sha256
    assert intent.verification_sha256 == verification.verification_sha256


def test_payload_is_bound_but_not_retained_in_intent_or_evidence():
    payload = b'{"private":"semantic material"}'
    intent = make_intent(payload=payload)
    evidence = intent.reportable_evidence()

    assert intent.payload_bytes == len(payload)
    assert payload.decode() not in repr(intent)
    assert payload.decode() not in repr(evidence)
    assert not hasattr(intent, "payload")
    assert evidence.payload_sha256 == intent.payload_sha256


def test_candidate_statement_is_not_copied_into_update_intent_or_evidence():
    secret_statement = "PRIVATE-CANDIDATE-SEMANTIC-TEXT"
    candidate = make_candidate(statement=secret_statement)
    intent = make_intent(candidate=candidate, verification=make_verification(candidate))

    assert secret_statement not in repr(intent)
    assert secret_statement not in repr(intent.reportable_evidence())


def test_transient_payload_must_match_exact_length_and_digest():
    intent = make_intent(payload=b"alpha")

    intent.assert_payload_matches(b"alpha")
    with pytest.raises(ValueError, match="length"):
        intent.assert_payload_matches(b"alph")
    with pytest.raises(ValueError, match="digest"):
        intent.assert_payload_matches(b"bravo")


def test_evidence_hashes_scope_instead_of_exposing_scope_ids():
    candidate = make_candidate(workspace_id="workspace-private", agent_id="agent-private")
    intent = make_intent(candidate=candidate, verification=make_verification(candidate))
    evidence = intent.reportable_evidence()

    assert isinstance(evidence, LearningUpdateEvidence)
    assert not hasattr(evidence, "workspace_id")
    assert not hasattr(evidence, "agent_id")
    assert "workspace-private" not in repr(evidence)
    assert "agent-private" not in repr(evidence)
    assert len(evidence.scope_sha256) == 64


def test_intent_digest_binds_target_candidate_verification_and_precondition():
    base = make_intent()
    target_changed = make_intent(target=LearningUpdateTarget.SKILL)
    revision_changed = make_intent(expected_revision_sha256=REVISION)
    other_candidate = make_candidate(candidate_id="candidate-002", statement="different")
    candidate_changed = make_intent(
        candidate=other_candidate,
        verification=make_verification(other_candidate),
    )

    assert len(
        {
            base.intent_sha256,
            target_changed.intent_sha256,
            revision_changed.intent_sha256,
            candidate_changed.intent_sha256,
        }
    ) == 4


def test_intent_digest_is_deterministic_for_same_verified_inputs():
    first = make_intent()
    second = make_intent()

    assert first == second
    assert first.intent_sha256 == second.intent_sha256
    assert first.reportable_evidence() == second.reportable_evidence()


def test_expected_revision_is_optional_but_exact_when_present():
    assert make_intent().expected_revision_sha256 is None
    assert make_intent(expected_revision_sha256=REVISION).expected_revision_sha256 == REVISION

    with pytest.raises(ValueError, match="expected_revision_sha256"):
        make_intent(expected_revision_sha256="D" * 64)


def test_intent_and_evidence_are_immutable():
    intent = make_intent()
    evidence = intent.reportable_evidence()

    with pytest.raises(FrozenInstanceError):
        intent.intent_id = "other"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        evidence.payload_bytes = 1  # type: ignore[misc]


def test_rejected_cognition_receipt_cannot_mint_update_intent():
    candidate = make_candidate()
    rejected = make_verification(candidate, passed=False)

    with pytest.raises(ValueError, match="requires VERIFIED"):
        make_intent(candidate=candidate, verification=rejected)


def test_verification_for_different_candidate_cannot_be_substituted():
    candidate_a = make_candidate(candidate_id="candidate-a", statement="A")
    candidate_b = make_candidate(candidate_id="candidate-b", statement="B")
    verification_a = make_verification(candidate_a)

    with pytest.raises(ValueError, match="candidate"):
        make_intent(candidate=candidate_b, verification=verification_a)


def test_forged_exact_verification_is_revalidated_before_update_binding():
    candidate = make_candidate()
    good = make_verification(candidate)
    forged = object.__new__(CognitionVerification)
    object.__setattr__(forged, "candidate_sha256", E)
    object.__setattr__(forged, "verification_policy_sha256", good.verification_policy_sha256)
    object.__setattr__(forged, "requirements", good.requirements)
    object.__setattr__(forged, "checks", good.checks)

    with pytest.raises(ValueError, match="candidate"):
        make_intent(candidate=candidate, verification=forged)


def test_forged_exact_candidate_scope_is_revalidated_before_update_binding():
    good = make_candidate()
    forged = object.__new__(CognitionCandidate)
    object.__setattr__(forged, "candidate_id", good.candidate_id)
    object.__setattr__(forged, "workspace_id", "invalid workspace")
    object.__setattr__(forged, "agent_id", good.agent_id)
    object.__setattr__(forged, "kind", good.kind)
    object.__setattr__(forged, "statement", good.statement)
    object.__setattr__(forged, "evidence", good.evidence)

    with pytest.raises(ValueError, match="workspace_id"):
        LearningUpdateIntent.bind_payload(
            intent_id="update-001",
            candidate=forged,
            verification=make_verification(good),
            expected_verification_policy_sha256=POLICY,
            expected_requirements=requirements(),
            target=LearningUpdateTarget.MEMORY,
            target_ref_sha256=TARGET_REF,
            update_schema="schema-v1",
            payload=b"payload",
        )


def test_wrong_trusted_policy_rejects_existing_verification_receipt():
    candidate = make_candidate()
    verification = make_verification(candidate)

    with pytest.raises(ValueError, match="policy"):
        LearningUpdateIntent.bind_payload(
            intent_id="update-001",
            candidate=candidate,
            verification=verification,
            expected_verification_policy_sha256=E,
            expected_requirements=requirements(),
            target=LearningUpdateTarget.MEMORY,
            target_ref_sha256=TARGET_REF,
            update_schema="schema-v1",
            payload=b"payload",
        )


def test_wrong_trusted_requirements_reject_existing_verification_receipt():
    candidate = make_candidate()
    verification = make_verification(candidate)
    wrong = (
        CognitionVerificationRequirement(
            check_id="other-check",
            verifier_sha256=VERIFIER,
        ),
    )

    with pytest.raises(ValueError, match="requirements"):
        LearningUpdateIntent.bind_payload(
            intent_id="update-001",
            candidate=candidate,
            verification=verification,
            expected_verification_policy_sha256=POLICY,
            expected_requirements=wrong,
            target=LearningUpdateTarget.MEMORY,
            target_ref_sha256=TARGET_REF,
            update_schema="schema-v1",
            payload=b"payload",
        )


def test_raw_digest_and_scope_factory_arguments_are_not_accepted():
    candidate = make_candidate()
    verification = make_verification(candidate)

    with pytest.raises(TypeError, match="unexpected keyword"):
        LearningUpdateIntent.bind_payload(
            intent_id="update-001",
            candidate=candidate,
            verification=verification,
            expected_verification_policy_sha256=POLICY,
            expected_requirements=requirements(),
            target=LearningUpdateTarget.MEMORY,
            target_ref_sha256=TARGET_REF,
            update_schema="schema-v1",
            payload=b"payload",
            candidate_sha256=A,  # type: ignore[call-arg]
        )


def test_received_intent_revalidates_against_cognition_and_transient_payload():
    candidate = make_candidate()
    verification = make_verification(candidate)
    intent = make_intent(candidate=candidate, verification=verification, payload=b"semantic-bytes")

    rebuilt = LearningUpdateIntent.revalidate(
        intent,
        candidate=candidate,
        verification=verification,
        expected_verification_policy_sha256=POLICY,
        expected_requirements=requirements(),
        payload=b"semantic-bytes",
    )

    assert rebuilt == intent


def test_forged_exact_intent_cannot_cross_revalidation_boundary():
    candidate = make_candidate()
    verification = make_verification(candidate)
    good = make_intent(candidate=candidate, verification=verification)
    forged = object.__new__(LearningUpdateIntent)
    for field in (
        "intent_id",
        "workspace_id",
        "agent_id",
        "target",
        "target_ref_sha256",
        "candidate_sha256",
        "verification_sha256",
        "update_schema",
        "payload_sha256",
        "payload_bytes",
        "expected_revision_sha256",
    ):
        object.__setattr__(forged, field, getattr(good, field))
    object.__setattr__(forged, "candidate_sha256", E)

    with pytest.raises(ValueError, match="trusted cognition"):
        LearningUpdateIntent.revalidate(
            forged,
            candidate=candidate,
            verification=verification,
            expected_verification_policy_sha256=POLICY,
            expected_requirements=requirements(),
            payload=b'{"fact":"bounded"}',
        )


def test_revalidation_rejects_payload_substitution():
    candidate = make_candidate()
    verification = make_verification(candidate)
    intent = make_intent(candidate=candidate, verification=verification, payload=b"original")

    with pytest.raises(ValueError, match="trusted cognition"):
        LearningUpdateIntent.revalidate(
            intent,
            candidate=candidate,
            verification=verification,
            expected_verification_policy_sha256=POLICY,
            expected_requirements=requirements(),
            payload=b"changed!",
        )


def test_direct_raw_field_constructor_is_not_public_update_authority():
    with pytest.raises(TypeError):
        LearningUpdateIntent(
            intent_id="update-001",
            workspace_id="workspace",
            agent_id="agent",
            target=LearningUpdateTarget.MEMORY,
            target_ref_sha256=A,
            candidate_sha256=B,
            verification_sha256=C,
            update_schema="schema-v1",
            payload_sha256=D,
            payload_bytes=1,
        )


class EvilStr(str):
    pass


class EvilBytes(bytes):
    pass


def test_payload_bytes_subclass_fails_before_hashing():
    with pytest.raises(TypeError, match="exact built-in bytes"):
        make_intent(payload=EvilBytes(b"payload"))


def test_payload_size_is_bounded_before_binding():
    with pytest.raises(ValueError, match="must not be empty"):
        make_intent(payload=b"")

    with pytest.raises(ValueError, match="exceeds"):
        make_intent(payload=b"x" * (16 * 1024 * 1024 + 1))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("intent_id", EvilStr("update-001")),
        ("target_ref_sha256", EvilStr(TARGET_REF)),
        ("update_schema", EvilStr("schema-v1")),
        ("expected_revision_sha256", EvilStr(REVISION)),
    ],
)
def test_remaining_authority_strings_reject_spoofable_subclasses(field, value):
    candidate = make_candidate()
    verification = make_verification(candidate)
    kwargs = {
        "intent_id": "update-001",
        "candidate": candidate,
        "verification": verification,
        "expected_verification_policy_sha256": POLICY,
        "expected_requirements": requirements(),
        "target": LearningUpdateTarget.MEMORY,
        "target_ref_sha256": TARGET_REF,
        "update_schema": "schema-v1",
        "payload": b"payload",
        "expected_revision_sha256": None,
    }
    kwargs[field] = value

    with pytest.raises(TypeError, match="exact built-in str"):
        LearningUpdateIntent.bind_payload(**kwargs)


@pytest.mark.parametrize(
    "bad_machine_id",
    ["", " has-space", "has space", "x" * 129, "schema\nv1"],
)
def test_machine_identifiers_are_bounded_and_log_safe(bad_machine_id):
    candidate = make_candidate()
    verification = make_verification(candidate)

    with pytest.raises(ValueError):
        LearningUpdateIntent.bind_payload(
            intent_id=bad_machine_id,
            candidate=candidate,
            verification=verification,
            expected_verification_policy_sha256=POLICY,
            expected_requirements=requirements(),
            target=LearningUpdateTarget.MEMORY,
            target_ref_sha256=TARGET_REF,
            update_schema="schema-v1",
            payload=b"payload",
        )


def test_wrong_target_type_is_rejected_even_if_text_matches_enum_value():
    candidate = make_candidate()
    verification = make_verification(candidate)

    with pytest.raises(TypeError, match="LearningUpdateTarget"):
        LearningUpdateIntent.bind_payload(
            intent_id="update-001",
            candidate=candidate,
            verification=verification,
            expected_verification_policy_sha256=POLICY,
            expected_requirements=requirements(),
            target="memory",  # type: ignore[arg-type]
            target_ref_sha256=TARGET_REF,
            update_schema="schema-v1",
            payload=b"payload",
        )


def test_factory_subclass_cannot_claim_canonical_update_authority():
    class DerivedIntent(LearningUpdateIntent):
        pass

    candidate = make_candidate()
    verification = make_verification(candidate)
    with pytest.raises(TypeError, match="canonical type"):
        DerivedIntent.bind_payload(
            intent_id="update-001",
            candidate=candidate,
            verification=verification,
            expected_verification_policy_sha256=POLICY,
            expected_requirements=requirements(),
            target=LearningUpdateTarget.MEMORY,
            target_ref_sha256=TARGET_REF,
            update_schema="schema-v1",
            payload=b"payload",
        )


def test_reportable_evidence_revalidates_trust_boundary():
    with pytest.raises(TypeError, match="exact built-in str"):
        LearningUpdateEvidence(
            intent_sha256=EvilStr(A),
            scope_sha256=B,
            target=LearningUpdateTarget.MEMORY,
            target_ref_sha256=C,
            candidate_sha256=D,
            verification_sha256=E,
            update_schema="schema-v1",
            payload_sha256=A,
            payload_bytes=1,
            expected_revision_sha256=None,
        )


def test_errors_do_not_echo_payload_contents():
    secret = b"api_key=DO-NOT-ECHO"
    intent = make_intent(payload=b"safe")

    with pytest.raises(ValueError) as exc_info:
        intent.assert_payload_matches(secret)

    assert "DO-NOT-ECHO" not in str(exc_info.value)
