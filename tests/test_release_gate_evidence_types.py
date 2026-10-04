from __future__ import annotations

from dataclasses import replace

import pytest

from nika_core.qa.release_gate import ReleaseGateEvidence, evaluate_release_gate

_AUTOMATED_FIELDS = (
    "core_ci_green",
    "windows_package_built",
    "package_smoke_passed",
    "manifest_verified",
    "third_party_notices_verified",
    "recovery_drill_passed",
    "packaged_uia_passed",
)


def _verified(**overrides: object) -> ReleaseGateEvidence:
    values: dict[str, object] = {field: True for field in _AUTOMATED_FIELDS}
    values.update(human_tested=True, nvda_verified=True)
    values.update(overrides)
    return ReleaseGateEvidence(**values)


def test_exact_boolean_evidence_preserves_production_gate() -> None:
    result = evaluate_release_gate(_verified())
    assert result.stage == "NVDA_VERIFIED"
    assert result.release_candidate_ready is True
    assert result.production_release_ready is True
    assert result.blockers == ()


def test_valid_pre_human_candidate_does_not_claim_production_release() -> None:
    result = evaluate_release_gate(_verified(human_tested=False, nvda_verified=False))
    assert result.stage == "PACKAGED"
    assert result.release_candidate_ready is True
    assert result.production_release_ready is False


@pytest.mark.parametrize("field", (*_AUTOMATED_FIELDS, "human_tested", "nvda_verified"))
@pytest.mark.parametrize("invalid", ["false", 1, 0, None, [], {}])
def test_non_boolean_evidence_fails_closed(field: str, invalid: object) -> None:
    result = evaluate_release_gate(replace(_verified(), **{field: invalid}))
    assert result.release_candidate_ready is False
    assert result.production_release_ready is False
    assert type(result.release_candidate_ready) is bool
    assert type(result.production_release_ready) is bool
    assert f"Invalid release evidence type: {field}" in result.blockers
    if field == "human_tested":
        assert result.stage != "NVDA_VERIFIED"
    if field == "windows_package_built":
        assert result.stage != "PACKAGED"


def test_nvda_evidence_without_human_acceptance_is_not_production() -> None:
    result = evaluate_release_gate(_verified(human_tested=False))
    assert result.release_candidate_ready is True
    assert result.production_release_ready is False
    assert "NVDA_VERIFIED cannot precede HUMAN_TESTED" in result.blockers


def test_missing_dataclass_field_cannot_qualify_release() -> None:
    evidence = object.__new__(ReleaseGateEvidence)
    result = evaluate_release_gate(evidence)
    assert result.release_candidate_ready is False
    assert result.production_release_ready is False
    assert len(result.blockers) >= 9
