from __future__ import annotations

from nika_core.model_gateway.gateway import model_identity_fingerprint
from nika_core.v01_packaged_team_state import V01PackagedTeamStateProvider

_TASK_ID = "task-1"
_TEAM_ID = "team-1"
_CHECKER_ID = "checker"
_EXPECTED = {
    "status": "agree",
    "sources": [{"state": "valid"}, {"state": "valid"}],
}


def _payload(
    *,
    provider_id: str,
    provider_kind: str,
    model: str,
    intelligence_mode: str,
) -> dict[str, object]:
    return {
        "checker_summary": _EXPECTED,
        "model_analysis": {
            "text": "Bounded model synthesis.",
            "provider_id": provider_id,
            "provider_kind": provider_kind,
            "model": model,
        },
        "model_analysis_provenance": {
            "schema": "nika.intelligence.provenance.v1",
            "origin": "model",
            "intelligence_mode": intelligence_mode,
            "provider_kind": provider_kind,
            "provider_id": provider_id,
            "model_fingerprint": model_identity_fingerprint(model),
            "request_correlation_id": f"{_TASK_ID}:v01:{_TEAM_ID}:{_CHECKER_ID}",
            "status": "succeeded",
        },
    }


def _valid(
    payload: dict[str, object],
    *,
    frozen_model_identity: tuple[str, str, str],
) -> bool:
    return V01PackagedTeamStateProvider._valid_persisted_checker_payload(
        payload,
        expected=_EXPECTED,
        shared_task_id=_TASK_ID,
        team_id=_TEAM_ID,
        root_id=_CHECKER_ID,
        model_required=True,
        frozen_model_identity=frozen_model_identity,
    )


def test_ollama_result_provenance_mode_must_match_frozen_route() -> None:
    model = "fixture-ollama-model"
    frozen = ("ollama", "local", model_identity_fingerprint(model))

    assert _valid(
        _payload(
            provider_id="ollama",
            provider_kind="local",
            model=model,
            intelligence_mode="external_local",
        ),
        frozen_model_identity=frozen,
    )
    assert not _valid(
        _payload(
            provider_id="ollama",
            provider_kind="local",
            model=model,
            intelligence_mode="embedded_local",
        ),
        frozen_model_identity=frozen,
    )


def test_foundry_result_provenance_mode_must_match_frozen_route() -> None:
    model = "fixture-foundry-model"
    frozen = ("foundry-local", "local", model_identity_fingerprint(model))

    assert _valid(
        _payload(
            provider_id="foundry-local",
            provider_kind="local",
            model=model,
            intelligence_mode="embedded_local",
        ),
        frozen_model_identity=frozen,
    )
    assert not _valid(
        _payload(
            provider_id="foundry-local",
            provider_kind="local",
            model=model,
            intelligence_mode="external_local",
        ),
        frozen_model_identity=frozen,
    )
