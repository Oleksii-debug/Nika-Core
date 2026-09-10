from __future__ import annotations

from dataclasses import replace

import pytest

from nika_core.experiments.contracts import PromotionPolicy
from nika_core.model_engineering import (
    QUALITY_METRIC,
    EvaluationCase,
    EvaluationPurpose,
    EvaluationSet,
    ModelCandidate,
    build_experiment_definition,
)
from nika_core.model_gateway.contracts import ModelMessage, PrivacyClass, ProviderKind


def _case(case_id: str, prompt: str, expected: str) -> EvaluationCase:
    return EvaluationCase(
        case_id=case_id,
        messages=(ModelMessage("user", prompt),),
        expected_text=expected,
    )


def _evaluation(
    *,
    purpose: EvaluationPurpose,
    cases: tuple[EvaluationCase, ...],
    evaluation_set_id: str,
) -> EvaluationSet:
    return EvaluationSet(
        evaluation_set_id=evaluation_set_id,
        version="v1",
        provenance_ref=f"dataset:{evaluation_set_id}",
        license_ref="license:synthetic-internal",
        purpose=purpose,
        privacy=PrivacyClass.PUBLIC,
        cases=cases,
    )


def _candidate(candidate_id: str, model: str) -> ModelCandidate:
    return ModelCandidate(
        candidate_id=candidate_id,
        provider_id="fake-local",
        provider_kind=ProviderKind.LOCAL,
        request_model=model,
        expected_response_model=model,
        engine_provenance_ref="engine:fake-local",
        engine_license_ref="license:engine",
        model_provenance_ref=f"model:{model}",
        model_license_ref="license:model",
    )


def test_held_out_record_cannot_be_reclassified_as_development_tuning_input() -> None:
    held_out_case = _case("held-1", "held prompt", "HELD_OUT_EXPECTED_SECRET")
    held_out = _evaluation(
        purpose=EvaluationPurpose.HELD_OUT,
        cases=(held_out_case,),
        evaluation_set_id="held-out",
    )

    # The same record, including its expected outcome, must not be admissible as
    # development/tuning material merely by wrapping it in another split label.
    with pytest.raises(ValueError, match="held-out"):
        replace(
            held_out,
            evaluation_set_id="tuning",
            provenance_ref="dataset:tuning",
            purpose=EvaluationPurpose.DEVELOPMENT,
        )


def test_promotion_replay_evidence_explicitly_names_held_out_split() -> None:
    evaluation = _evaluation(
        purpose=EvaluationPurpose.HELD_OUT,
        cases=(_case("held-1", "prompt", "expected"),),
        evaluation_set_id="held-out",
    )
    definition = build_experiment_definition(
        experiment_id="promotion-with-split-evidence",
        champion=_candidate("champion", "m1"),
        challengers=(_candidate("challenger", "m2"),),
        evaluation_set=evaluation,
        policy=PromotionPolicy(primary_metric=QUALITY_METRIC, minimum_replays=1),
        permission_fingerprint="permissions-v1",
    )

    assert definition.replays
    for replay in definition.replays:
        assert "held_out" in replay.dataset_ref
