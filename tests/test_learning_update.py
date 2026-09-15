from dataclasses import FrozenInstanceError

import pytest

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


def make_intent(
    *,
    target: LearningUpdateTarget = LearningUpdateTarget.MEMORY,
    payload: bytes = b'{"fact":"bounded"}',
    expected_revision_sha256: str | None = None,
) -> LearningUpdateIntent:
    return LearningUpdateIntent.bind_payload(
        intent_id="update-001",
        workspace_id="Проєкт Ніка",
        agent_id="agent-1",
        target=target,
        target_ref_sha256=A,
        candidate_sha256=B,
        verification_sha256=C,
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


def test_payload_is_bound_but_not_retained_in_intent_or_evidence():
    payload = b'{"private":"semantic material"}'
    intent = make_intent(payload=payload)
    evidence = intent.reportable_evidence()

    assert intent.payload_bytes == len(payload)
    assert payload.decode() not in repr(intent)
    assert payload.decode() not in repr(evidence)
    assert not hasattr(intent, "payload")
    assert evidence.payload_sha256 == intent.payload_sha256


def test_transient_payload_must_match_exact_length_and_digest():
    intent = make_intent(payload=b"alpha")

    intent.assert_payload_matches(b"alpha")
    with pytest.raises(ValueError, match="length"):
        intent.assert_payload_matches(b"alph")
    with pytest.raises(ValueError, match="digest"):
        intent.assert_payload_matches(b"bravo")


def test_evidence_hashes_scope_instead_of_exposing_scope_ids():
    intent = make_intent()
    evidence = intent.reportable_evidence()

    assert isinstance(evidence, LearningUpdateEvidence)
    assert not hasattr(evidence, "workspace_id")
    assert not hasattr(evidence, "agent_id")
    assert "Проєкт Ніка" not in repr(evidence)
    assert "agent-1" not in repr(evidence)
    assert len(evidence.scope_sha256) == 64


def test_intent_digest_binds_target_candidate_verification_and_precondition():
    base = make_intent()
    target_changed = make_intent(target=LearningUpdateTarget.SKILL)
    revision_changed = make_intent(expected_revision_sha256=D)

    candidate_changed = LearningUpdateIntent.bind_payload(
        intent_id="update-001",
        workspace_id="Проєкт Ніка",
        agent_id="agent-1",
        target=LearningUpdateTarget.MEMORY,
        target_ref_sha256=A,
        candidate_sha256=E,
        verification_sha256=C,
        update_schema="nika.semantic-update/v1",
        payload=b'{"fact":"bounded"}',
    )

    assert len(
        {
            base.intent_sha256,
            target_changed.intent_sha256,
            revision_changed.intent_sha256,
            candidate_changed.intent_sha256,
        }
    ) == 4


def test_expected_revision_is_optional_but_exact_when_present():
    assert make_intent().expected_revision_sha256 is None
    assert make_intent(expected_revision_sha256=D).expected_revision_sha256 == D

    with pytest.raises(ValueError, match="expected_revision_sha256"):
        make_intent(expected_revision_sha256="D" * 64)


def test_intent_and_evidence_are_immutable():
    intent = make_intent()
    evidence = intent.reportable_evidence()

    with pytest.raises(FrozenInstanceError):
        intent.intent_id = "other"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        evidence.payload_bytes = 1  # type: ignore[misc]


class EvilStr(str):
    pass


class EvilBytes(bytes):
    pass


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("intent_id", EvilStr("update-001")),
        ("workspace_id", EvilStr("workspace")),
        ("agent_id", EvilStr("agent")),
        ("target_ref_sha256", EvilStr(A)),
        ("candidate_sha256", EvilStr(B)),
        ("verification_sha256", EvilStr(C)),
        ("update_schema", EvilStr("schema-v1")),
        ("expected_revision_sha256", EvilStr(D)),
    ],
)
def test_authority_bearing_string_subclasses_fail_closed(field, value):
    kwargs = {
        "intent_id": "update-001",
        "workspace_id": "workspace",
        "agent_id": "agent",
        "target": LearningUpdateTarget.MEMORY,
        "target_ref_sha256": A,
        "candidate_sha256": B,
        "verification_sha256": C,
        "update_schema": "schema-v1",
        "payload": b"payload",
        "expected_revision_sha256": None,
    }
    kwargs[field] = value

    with pytest.raises(TypeError, match="exact built-in str"):
        LearningUpdateIntent.bind_payload(**kwargs)


def test_payload_bytes_subclass_fails_before_hashing():
    with pytest.raises(TypeError, match="exact built-in bytes"):
        make_intent(payload=EvilBytes(b"payload"))


def test_payload_size_is_bounded_before_binding():
    with pytest.raises(ValueError, match="must not be empty"):
        make_intent(payload=b"")

    with pytest.raises(ValueError, match="exceeds"):
        make_intent(payload=b"x" * (16 * 1024 * 1024 + 1))


@pytest.mark.parametrize(
    "bad_scope",
    [
        " workspace",
        "workspace ",
        "line\nbreak",
        "a" * 257,
        "e\u0301",
    ],
)
def test_scope_ids_fail_closed_when_not_canonical(bad_scope):
    with pytest.raises(ValueError):
        LearningUpdateIntent.bind_payload(
            intent_id="update-001",
            workspace_id=bad_scope,
            agent_id="agent",
            target=LearningUpdateTarget.MEMORY,
            target_ref_sha256=A,
            candidate_sha256=B,
            verification_sha256=C,
            update_schema="schema-v1",
            payload=b"payload",
        )


@pytest.mark.parametrize(
    "bad_machine_id",
    ["", " has-space", "has space", "x" * 129, "schema\nv1"],
)
def test_machine_identifiers_are_bounded_and_log_safe(bad_machine_id):
    with pytest.raises(ValueError):
        LearningUpdateIntent.bind_payload(
            intent_id=bad_machine_id,
            workspace_id="workspace",
            agent_id="agent",
            target=LearningUpdateTarget.MEMORY,
            target_ref_sha256=A,
            candidate_sha256=B,
            verification_sha256=C,
            update_schema="schema-v1",
            payload=b"payload",
        )


def test_wrong_target_type_is_rejected_even_if_text_matches_enum_value():
    with pytest.raises(TypeError, match="LearningUpdateTarget"):
        LearningUpdateIntent.bind_payload(
            intent_id="update-001",
            workspace_id="workspace",
            agent_id="agent",
            target="memory",  # type: ignore[arg-type]
            target_ref_sha256=A,
            candidate_sha256=B,
            verification_sha256=C,
            update_schema="schema-v1",
            payload=b"payload",
        )


def test_direct_constructor_rejects_bool_as_payload_length():
    with pytest.raises(TypeError, match="exact built-in int"):
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
            payload_bytes=True,  # type: ignore[arg-type]
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
