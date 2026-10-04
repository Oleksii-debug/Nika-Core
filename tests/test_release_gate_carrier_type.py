"""Release clearance never accepts duck-typed or subclass-supplied evidence."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from nika_core.qa.release_gate import ReleaseGateEvidence, evaluate_release_gate

_FIELDS = (
    "core_ci_green",
    "windows_package_built",
    "package_smoke_passed",
    "manifest_verified",
    "third_party_notices_verified",
    "recovery_drill_passed",
    "packaged_uia_passed",
    "human_tested",
    "nvda_verified",
)


def _all_green() -> ReleaseGateEvidence:
    return ReleaseGateEvidence(**{field: True for field in _FIELDS})


@pytest.mark.parametrize(
    "carrier",
    [
        pytest.param(SimpleNamespace(**{field: True for field in _FIELDS}), id="namespace"),
        pytest.param({field: True for field in _FIELDS}, id="mapping"),
        pytest.param(object(), id="plain-object"),
        pytest.param(None, id="none"),
    ],
)
def test_noncanonical_carrier_cannot_certify_release(carrier: object) -> None:
    result = evaluate_release_gate(carrier)  # type: ignore[arg-type]
    assert result.stage == "IMPLEMENTED"
    assert result.release_candidate_ready is False
    assert result.production_release_ready is False
    assert result.blockers == ("Invalid release evidence carrier",)


def test_release_gate_does_not_evaluate_untrusted_getters() -> None:
    class DangerousCarrier:
        def __getattr__(self, _name: str) -> object:
            raise AssertionError("release gate evaluated an untrusted getter")

    result = evaluate_release_gate(DangerousCarrier())  # type: ignore[arg-type]
    assert result.production_release_ready is False
    assert result.blockers == ("Invalid release evidence carrier",)


def test_evidence_subclass_cannot_bypass_exact_carrier_type() -> None:
    class ForgedCarrier(ReleaseGateEvidence):
        pass

    forged = ForgedCarrier(**{field: True for field in _FIELDS})
    result = evaluate_release_gate(forged)
    assert result.production_release_ready is False
    assert result.blockers == ("Invalid release evidence carrier",)


def test_genuine_evidence_preserves_pre_human_and_production_decisions() -> None:
    genuine = _all_green()
    result = evaluate_release_gate(genuine)
    assert result.release_candidate_ready is True
    assert result.production_release_ready is True
    assert result.blockers == ()

    prehuman = replace(genuine, human_tested=False, nvda_verified=False)
    prehuman_result = evaluate_release_gate(prehuman)
    assert prehuman_result.release_candidate_ready is True
    assert prehuman_result.production_release_ready is False
    assert prehuman_result.stage == "PACKAGED"
