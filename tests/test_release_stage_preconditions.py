"""Human/NVDA release stages require the earlier automated release gate."""

from dataclasses import replace

import pytest

from nika_core.qa.release_gate import ReleaseGateEvidence, evaluate_release_gate

_AUTOMATED = (
    "core_ci_green",
    "windows_package_built",
    "package_smoke_passed",
    "manifest_verified",
    "third_party_notices_verified",
    "recovery_drill_passed",
    "packaged_uia_passed",
)


def _complete() -> ReleaseGateEvidence:
    return ReleaseGateEvidence(**{field: True for field in (
        *_AUTOMATED, "human_tested", "nvda_verified"
    )})


@pytest.mark.parametrize("missing", _AUTOMATED)
def test_human_and_nvda_flags_cannot_advance_incomplete_automated_release(
    missing: str,
) -> None:
    evidence = replace(_complete(), **{missing: False})
    result = evaluate_release_gate(evidence)
    assert result.release_candidate_ready is False
    assert result.production_release_ready is False
    assert result.stage not in ("HUMAN_TESTED", "NVDA_VERIFIED")
    assert result.blockers


@pytest.mark.parametrize("invalid", ["true", 1, None])
def test_invalid_automated_evidence_cannot_advance_human_stage(invalid: object) -> None:
    evidence = replace(_complete(), recovery_drill_passed=invalid)
    result = evaluate_release_gate(evidence)
    assert result.release_candidate_ready is False
    assert result.production_release_ready is False
    assert result.stage == "PACKAGED"
    assert "Invalid release evidence type: recovery_drill_passed" in result.blockers


def test_human_and_nvda_stage_advances_only_after_prior_release_gate() -> None:
    complete = _complete()
    human_only = replace(complete, nvda_verified=False)
    prehuman = replace(human_only, human_tested=False)
    assert evaluate_release_gate(prehuman).stage == "PACKAGED"
    assert evaluate_release_gate(human_only).stage == "HUMAN_TESTED"
    accepted = evaluate_release_gate(complete)
    assert accepted.stage == "NVDA_VERIFIED"
    assert accepted.release_candidate_ready is True
    assert accepted.production_release_ready is True


def test_nvda_flag_without_human_acceptance_stays_prehuman() -> None:
    evidence = replace(_complete(), human_tested=False)
    result = evaluate_release_gate(evidence)
    assert result.stage == "PACKAGED"
    assert result.production_release_ready is False
    assert "NVDA_VERIFIED cannot precede HUMAN_TESTED" in result.blockers
