from __future__ import annotations

from dataclasses import fields

import pytest

from nika_core.qa.release_gate import ReleaseGateEvidence, evaluate_release_gate


@pytest.mark.parametrize("field", [item.name for item in fields(ReleaseGateEvidence)])
@pytest.mark.parametrize("invalid", (0, 1, "false", "true", None, [], {}))
def test_release_gate_rejects_nonboolean_evidence_on_construction(
    field: str, invalid: object
) -> None:
    with pytest.raises(TypeError, match=field):
        ReleaseGateEvidence(**{field: invalid})


@pytest.mark.parametrize(
    "field",
    ("core_ci_green", "packaged_uia_passed", "human_tested", "nvda_verified"),
)
@pytest.mark.parametrize("invalid", (1, "false", "true", None))
def test_release_gate_rejects_mutated_evidence_before_any_release_claim(
    field: str, invalid: object
) -> None:
    evidence = ReleaseGateEvidence()
    object.__setattr__(evidence, field, invalid)

    with pytest.raises(TypeError, match=field):
        evaluate_release_gate(evidence)


def test_release_gate_rejects_non_evidence_object() -> None:
    with pytest.raises(TypeError, match="ReleaseGateEvidence"):
        evaluate_release_gate(object())  # type: ignore[arg-type]


def test_exact_boolean_evidence_preserves_existing_release_decisions() -> None:
    absent = evaluate_release_gate(ReleaseGateEvidence())
    assert not absent.release_candidate_ready
    assert not absent.production_release_ready

    complete = ReleaseGateEvidence(
        **{item.name: True for item in fields(ReleaseGateEvidence)}
    )
    qualified = evaluate_release_gate(complete)
    assert qualified.release_candidate_ready is True
    assert qualified.production_release_ready is True
    assert qualified.stage == "NVDA_VERIFIED"
