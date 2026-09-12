from __future__ import annotations

from copy import deepcopy

import pytest

from nika_core.model_gateway.gateway import model_identity_fingerprint
from nika_core.v01_packaged_team_state import V01PackagedTeamStateProvider


_TASK_ID = "task-1"
_TEAM_ID = "team-1"
_CHECKER_ID = "checker"
_MODEL = "test-model"
_EXPECTED = {"status": "agree", "sources": [{"state": "valid"}, {"state": "valid"}]}


def _model_backed_payload() -> dict[str, object]:
    return {
        "checker_summary": deepcopy(_EXPECTED),
        "model_analysis": {
            "text": "Bounded model synthesis.",
            "provider_id": "ollama",
            "provider_kind": "local",
            "model": _MODEL,
        },
        "model_analysis_provenance": {
            "schema": "nika.intelligence.provenance.v1",
            "origin": "model",
            "intelligence_mode": "external_local",
            "provider_kind": "local",
            "provider_id": "ollama",
            "model_fingerprint": model_identity_fingerprint(_MODEL),
            "request_correlation_id": f"{_TASK_ID}:v01:{_TEAM_ID}:{_CHECKER_ID}",
            "status": "succeeded",
        },
    }


def _valid(payload: object) -> bool:
    return V01PackagedTeamStateProvider._valid_persisted_checker_payload(
        payload,
        expected=_EXPECTED,
        shared_task_id=_TASK_ID,
        team_id=_TEAM_ID,
        root_id=_CHECKER_ID,
    )


def test_checker_envelope_accepts_legacy_and_canonical_model_backed_result() -> None:
    assert _valid({"checker_summary": deepcopy(_EXPECTED)}) is True
    assert _valid(_model_backed_payload()) is True


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_top_level",
        "missing_provenance",
        "analysis_extra",
        "blank_text",
        "oversized_text",
        "provider_mismatch",
        "provider_kind_mismatch",
        "model_mismatch",
        "correlation_mismatch",
        "non_success_provenance",
        "provenance_extra",
    ],
)
def test_checker_envelope_rejects_unbound_or_malformed_model_evidence(mutation: str) -> None:
    payload = _model_backed_payload()
    analysis = payload["model_analysis"]
    provenance = payload["model_analysis_provenance"]
    assert isinstance(analysis, dict)
    assert isinstance(provenance, dict)

    if mutation == "unknown_top_level":
        payload["raw_model_output"] = "must never be accepted"
    elif mutation == "missing_provenance":
        del payload["model_analysis_provenance"]
    elif mutation == "analysis_extra":
        analysis["raw"] = "must never be accepted"
    elif mutation == "blank_text":
        analysis["text"] = "   "
    elif mutation == "oversized_text":
        analysis["text"] = "x" * 2001
    elif mutation == "provider_mismatch":
        analysis["provider_id"] = "different-provider"
    elif mutation == "provider_kind_mismatch":
        analysis["provider_kind"] = "cloud"
    elif mutation == "model_mismatch":
        analysis["model"] = "different-model"
    elif mutation == "correlation_mismatch":
        provenance["request_correlation_id"] = "foreign-task:v01:team-1:checker"
    elif mutation == "non_success_provenance":
        provenance["status"] = "failed"
    elif mutation == "provenance_extra":
        provenance["raw"] = "must never be accepted"
    else:  # pragma: no cover - parametrization is exhaustive.
        raise AssertionError(mutation)

    assert _valid(payload) is False


def test_checker_envelope_rejects_summary_rebinding() -> None:
    payload = _model_backed_payload()
    payload["checker_summary"] = {"status": "disagree"}
    assert _valid(payload) is False
