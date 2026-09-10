from __future__ import annotations

import asyncio

from nika_core.experiments.contracts import ArtifactKind, PromotionPolicy
from nika_core.model_engineering import (
    EvaluationCase,
    EvaluationPurpose,
    EvaluationSet,
    ModelBenchmarkRunner,
    ModelCandidate,
    build_experiment_definition,
)
from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelResponse,
    PrivacyClass,
    ProviderKind,
)


class _Clock:
    def __init__(self) -> None:
        self._values = iter((1.0, 1.001))

    def __call__(self) -> float:
        return next(self._values)


class _CaptureGateway:
    def __init__(self) -> None:
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return ModelResponse(
            request_id=request.request_id,
            text="ok",
            provider_id=request.provider_id,
            provider_kind=ProviderKind.LOCAL,
            model=request.model,
        )


def _candidate() -> ModelCandidate:
    return ModelCandidate(
        candidate_id="candidate-a",
        provider_id="local-a",
        provider_kind=ProviderKind.LOCAL,
        request_model="model-a",
        expected_response_model="model-a",
        engine_provenance_ref="engine:fixture",
        engine_license_ref="license:fixture-engine",
        model_provenance_ref="model:fixture",
        model_license_ref="license:fixture-model",
    )


def _evaluation_set() -> EvaluationSet:
    return EvaluationSet(
        evaluation_set_id="prompt-registry-qa",
        version="1",
        provenance_ref="dataset:synthetic-internal",
        license_ref="license:internal-fixture",
        purpose=EvaluationPurpose.HELD_OUT,
        privacy=PrivacyClass.PUBLIC,
        cases=(
            EvaluationCase(
                case_id="case-a",
                messages=(ModelMessage("user", "synthetic input"),),
                expected_text="ok",
            ),
        ),
    )


def test_benchmark_request_binds_explicit_prompt_variant_identity() -> None:
    gateway = _CaptureGateway()
    report = asyncio.run(
        ModelBenchmarkRunner(gateway, clock=_Clock()).benchmark(
            _candidate(),
            _evaluation_set(),
        )
    )

    assert report.case_results[0].passed
    assert len(gateway.requests) == 1
    metadata = gateway.requests[0].metadata
    required = {
        "prompt_variant_id",
        "prompt_variant_version",
        "prompt_variant_sha256",
    }
    assert required <= set(metadata), (
        "benchmark request evidence does not identify the canonical prompt/strategy "
        "variant; different hidden prompt texts can therefore masquerade as the same run"
    )


def test_benchmark_report_preserves_prompt_variant_identity() -> None:
    gateway = _CaptureGateway()
    report = asyncio.run(
        ModelBenchmarkRunner(gateway, clock=_Clock()).benchmark(
            _candidate(),
            _evaluation_set(),
        )
    )

    missing = [
        name
        for name in (
            "prompt_variant_id",
            "prompt_variant_version",
            "prompt_variant_sha256",
        )
        if not hasattr(report, name)
    ]
    assert not missing, (
        "benchmark result drops canonical prompt/strategy identity: "
        + ", ".join(missing)
    )


def test_experiment_strategy_ref_is_not_model_config_substitution() -> None:
    definition = build_experiment_definition(
        experiment_id="prompt-registry-qa",
        champion=_candidate(),
        challengers=(
            ModelCandidate(
                candidate_id="candidate-b",
                provider_id="local-a",
                provider_kind=ProviderKind.LOCAL,
                request_model="model-a",
                expected_response_model="model-a",
                engine_provenance_ref="engine:fixture",
                engine_license_ref="license:fixture-engine",
                model_provenance_ref="model:fixture",
                model_license_ref="license:fixture-model",
            ),
        ),
        evaluation_set=_evaluation_set(),
        policy=PromotionPolicy(primary_metric="model_quality_score"),
        permission_fingerprint="permission:fixture",
    )

    assert definition.champion.artifact_kind in {
        ArtifactKind.PROMPT,
        ArtifactKind.STRATEGY,
    }, (
        "Experiment StrategyRef currently identifies only model CONFIG; it cannot prove "
        "which canonical prompt/strategy text produced benchmark evidence"
    )
