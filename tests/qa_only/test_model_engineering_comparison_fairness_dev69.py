from __future__ import annotations

import asyncio
import json

import pytest

from nika_core.model_engineering import (
    BenchmarkSuiteReport,
    EvaluationCase,
    EvaluationPurpose,
    EvaluationSet,
    ModelBenchmarkRunner,
    ModelCandidate,
    benchmark_report_json,
)
from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelResponse,
    PrivacyClass,
    ProviderKind,
)


class _Gateway:
    def __init__(self) -> None:
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return ModelResponse(
            request_id=request.request_id,
            text="expected",
            provider_id=request.provider_id,
            provider_kind=ProviderKind.LOCAL,
            model=request.model,
        )


class _Clock:
    def __init__(self, values: tuple[float, ...]) -> None:
        self._values = iter(values)

    def __call__(self) -> float:
        return next(self._values)


class _AlwaysZeroScorer:
    def score(self, case, response) -> float:
        del case, response
        return 0.0


def _candidate(candidate_id: str, model: str) -> ModelCandidate:
    return ModelCandidate(
        candidate_id=candidate_id,
        provider_id=f"provider-{candidate_id}",
        provider_kind=ProviderKind.LOCAL,
        request_model=model,
        expected_response_model=model,
        engine_provenance_ref="engine:fixture",
        engine_license_ref="license:engine",
        model_provenance_ref=f"model:{model}",
        model_license_ref="license:model",
    )


def _evaluation(*, version: str = "v1", prompt: str = "prompt") -> EvaluationSet:
    return EvaluationSet(
        evaluation_set_id="fairness-fixture",
        version=version,
        provenance_ref="dataset:fairness-fixture",
        license_ref="license:internal-test",
        purpose=EvaluationPurpose.HELD_OUT,
        privacy=PrivacyClass.PUBLIC,
        cases=(
            EvaluationCase(
                case_id="case-1",
                messages=(ModelMessage("user", prompt),),
                expected_text="expected",
            ),
        ),
    )


def _run_report(
    candidate: ModelCandidate,
    evaluation: EvaluationSet,
    *,
    timeout_seconds: float = 60.0,
    temperature: float | None = 0.0,
    scorer=None,
):
    gateway = _Gateway()
    runner = ModelBenchmarkRunner(
        gateway,
        scorer=scorer,
        clock=_Clock((1.0, 1.01)),
    )
    report = asyncio.run(
        runner.benchmark(
            candidate,
            evaluation,
            timeout_seconds=timeout_seconds,
            temperature=temperature,
        )
    )
    assert len(gateway.requests) == 1
    return report, gateway.requests[0]


def _suite(evaluation: EvaluationSet, reports) -> BenchmarkSuiteReport:
    return BenchmarkSuiteReport(
        evaluation_set_id=evaluation.evaluation_set_id,
        evaluation_set_version=evaluation.version,
        evaluation_set_sha256=evaluation.content_sha256,
        reports=tuple(reports),
    )


def _payload_keys(value) -> set[str]:
    if isinstance(value, dict):
        keys = set(value)
        for child in value.values():
            keys.update(_payload_keys(child))
        return keys
    if isinstance(value, list):
        keys: set[str] = set()
        for child in value:
            keys.update(_payload_keys(child))
        return keys
    return set()


def test_direct_suite_uses_same_prompt_and_generation_configuration() -> None:
    evaluation = _evaluation()
    gateway = _Gateway()
    runner = ModelBenchmarkRunner(
        gateway,
        clock=_Clock((1.0, 1.01, 2.0, 2.01)),
    )

    report = asyncio.run(
        runner.benchmark_suite(
            (
                _candidate("a", "model-a"),
                _candidate("b", "model-b"),
            ),
            evaluation,
            timeout_seconds=17.0,
            temperature=0.25,
        )
    )

    assert len(report.reports) == 2
    assert len(gateway.requests) == 2
    assert {request.timeout_seconds for request in gateway.requests} == {17.0}
    assert {request.temperature for request in gateway.requests} == {0.25}
    assert all(
        request.messages == evaluation.cases[0].messages for request in gateway.requests
    )


def test_comparison_rejects_dataset_version_drift() -> None:
    evaluation_a = _evaluation(version="v1")
    evaluation_b = _evaluation(version="v2")
    report_a, _ = _run_report(_candidate("a", "model-a"), evaluation_a)
    report_b, _ = _run_report(_candidate("b", "model-b"), evaluation_b)

    with pytest.raises(ValueError, match="mixes evaluation set identities"):
        _suite(evaluation_a, (report_a, report_b))


def test_comparison_rejects_prompt_drift_even_when_version_label_is_same() -> None:
    evaluation_a = _evaluation(version="v1", prompt="prompt variant a")
    evaluation_b = _evaluation(version="v1", prompt="prompt variant b")
    assert evaluation_a.content_sha256 != evaluation_b.content_sha256
    report_a, _ = _run_report(_candidate("a", "model-a"), evaluation_a)
    report_b, _ = _run_report(_candidate("b", "model-b"), evaluation_b)

    with pytest.raises(ValueError, match="mixes evaluation set identities"):
        _suite(evaluation_a, (report_a, report_b))


@pytest.mark.parametrize(
    ("timeout_a", "temperature_a", "timeout_b", "temperature_b"),
    (
        (17.0, 0.0, 17.0, 0.75),
        (17.0, 0.0, 23.0, 0.0),
    ),
)
def test_comparison_rejects_generation_configuration_drift(
    timeout_a: float,
    temperature_a: float,
    timeout_b: float,
    temperature_b: float,
) -> None:
    evaluation = _evaluation()
    report_a, request_a = _run_report(
        _candidate("a", "model-a"),
        evaluation,
        timeout_seconds=timeout_a,
        temperature=temperature_a,
    )
    report_b, request_b = _run_report(
        _candidate("b", "model-b"),
        evaluation,
        timeout_seconds=timeout_b,
        temperature=temperature_b,
    )
    assert (request_a.timeout_seconds, request_a.temperature) != (
        request_b.timeout_seconds,
        request_b.temperature,
    )

    with pytest.raises(ValueError, match="generation configuration"):
        _suite(evaluation, (report_a, report_b))


def test_comparison_rejects_scoring_method_drift() -> None:
    evaluation = _evaluation()
    report_a, _ = _run_report(_candidate("a", "model-a"), evaluation)
    report_b, _ = _run_report(
        _candidate("b", "model-b"),
        evaluation,
        scorer=_AlwaysZeroScorer(),
    )
    assert report_a.weighted_quality_score == 1.0
    assert report_b.weighted_quality_score == 0.0

    with pytest.raises(ValueError, match="scoring method"):
        _suite(evaluation, (report_a, report_b))


def test_report_evidence_names_generation_and_scoring_basis() -> None:
    evaluation = _evaluation()
    report, request = _run_report(
        _candidate("a", "model-a"),
        evaluation,
        timeout_seconds=17.0,
        temperature=0.25,
    )

    payload = json.loads(benchmark_report_json(report))
    keys = _payload_keys(payload)

    assert request.timeout_seconds == 17.0
    assert request.temperature == 0.25
    assert "timeout_seconds" in keys
    assert "temperature" in keys
    assert {
        "scorer_id",
        "scoring_method",
        "scoring_method_id",
    } & keys
