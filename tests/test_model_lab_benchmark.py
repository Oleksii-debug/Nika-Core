from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from nika_core.experiments.contracts import MetricRule, PromotionPolicy
from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    PrivacyClass,
    ProviderKind,
)
from nika_core.model_lab import (
    AttemptStatus,
    BenchmarkCase,
    BenchmarkSuite,
    ExactMatchScorer,
    MetricValue,
    ModelBenchmarkRunner,
    ModelCandidate,
    build_experiment_definition,
    evidence_document,
    metric_observations,
    suite_sha256,
)
from nika_core.resources.contracts import ResourceSnapshot


CHECKSUM = "a" * 64


@dataclass
class FakeGateway:
    response_text: str = "4"
    provider_id: str = "ollama"
    provider_kind: ProviderKind = ProviderKind.LOCAL
    model: str = "qwen3:8b"
    error: ModelGatewayError | None = None

    def __post_init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return ModelResponse(
            request_id=request.request_id,
            text=self.response_text,
            provider_id=self.provider_id,
            provider_kind=self.provider_kind,
            model=self.model,
            usage=ModelUsage(input_tokens=3, output_tokens=1, total_tokens=4),
            latency_ms=12.5,
        )


class FakeObserver:
    def __init__(self) -> None:
        self._index = 0

    def snapshot(self) -> ResourceSnapshot:
        self._index += 1
        return ResourceSnapshot(
            cpu_percent=10.0 + self._index,
            memory_percent=40.0 + self._index,
            available_memory_bytes=8_000_000_000 - self._index,
        )


class ReservedMetricScorer:
    scorer_id = "bad_scorer"

    def score(self, case: BenchmarkCase, response_text: str) -> tuple[MetricValue, ...]:
        del case, response_text
        return (MetricValue(metric="model_lab.fake", value=1.0),)


def candidate(*, provider_kind: ProviderKind = ProviderKind.LOCAL) -> ModelCandidate:
    return ModelCandidate(
        candidate_id="ollama-qwen3-8b",
        provider_id="ollama",
        provider_kind=provider_kind,
        model="qwen3:8b",
        model_version="2026-08",
        license_reference="license://qwen3",
        provenance_reference="provenance://ollama/qwen3",
        permission_fingerprint="perm-v1",
        artifact_sha256=CHECKSUM if provider_kind is ProviderKind.LOCAL else None,
    )


def suite(*, prompt: str = "2+2?", repetitions: int = 1) -> BenchmarkSuite:
    return BenchmarkSuite(
        suite_id="math-smoke",
        version="1.0.0",
        repetitions=repetitions,
        cases=(
            BenchmarkCase(
                case_id="arithmetic-1",
                messages=(ModelMessage(role="user", content=prompt),),
                dataset_ref="dataset://math-smoke",
                dataset_version="1",
                scorer_id="exact_match",
                privacy=PrivacyClass.PRIVATE,
                reference_text="4",
            ),
        ),
    )


def test_local_candidate_requires_checksum_evidence() -> None:
    with pytest.raises(ValueError, match="artifact_sha256"):
        ModelCandidate(
            candidate_id="candidate",
            provider_id="ollama",
            provider_kind=ProviderKind.LOCAL,
            model="qwen3:8b",
            model_version="1",
            license_reference="license://qwen3",
            provenance_reference="provenance://qwen3",
            permission_fingerprint="perm-v1",
        )


def test_suite_digest_binds_prompt_without_exposing_prompt() -> None:
    first = suite(prompt="private prompt alpha")
    second = suite(prompt="private prompt beta")
    assert suite_sha256(first) != suite_sha256(second)


def test_success_evidence_is_text_redacted_and_resource_measured() -> None:
    gateway = FakeGateway(response_text="private response")
    benchmark_suite = BenchmarkSuite(
        suite_id="redaction",
        version="1",
        cases=(
            BenchmarkCase(
                case_id="case-1",
                messages=(ModelMessage(role="user", content="private prompt"),),
                dataset_ref="dataset://redaction",
                dataset_version="1",
                scorer_id="exact_match",
                reference_text="private response",
            ),
        ),
    )
    runner = ModelBenchmarkRunner(gateway, resource_observer=FakeObserver())
    evidence = asyncio.run(
        runner.run(
            run_id="run-1",
            candidate=candidate(),
            suite=benchmark_suite,
            scorers={"exact_match": ExactMatchScorer()},
        )
    )

    assert evidence.complete is True
    attempt = evidence.attempts[0]
    assert attempt.status is AttemptStatus.SUCCESS
    assert attempt.response_sha256 is not None
    assert attempt.resource_before is not None
    assert attempt.resource_after is not None
    metrics = {item.metric: item.value for item in attempt.metrics}
    assert metrics["quality.exact_match"] == 1.0
    assert metrics["model_lab.total_tokens"] == 4.0
    assert metrics["model_lab.cpu_percent"] == 12.0
    serialized = evidence_document(evidence)
    assert "private prompt" not in serialized
    assert "private response" not in serialized
    assert gateway.requests[0].provider_id == "ollama"
    assert gateway.requests[0].fallback_provider_ids == ()


def test_cloud_benchmark_requires_explicit_opt_in() -> None:
    runner = ModelBenchmarkRunner(
        FakeGateway(provider_kind=ProviderKind.CLOUD, provider_id="cloud")
    )
    cloud = ModelCandidate(
        candidate_id="cloud-model",
        provider_id="cloud",
        provider_kind=ProviderKind.CLOUD,
        model="model-v1",
        model_version="2026-08-01",
        license_reference="license://cloud/model",
        provenance_reference="provider://cloud/model-v1",
        permission_fingerprint="perm-v1",
    )
    with pytest.raises(PermissionError, match="allow_cloud"):
        asyncio.run(
            runner.run(
                run_id="cloud-run",
                candidate=cloud,
                suite=suite(),
                scorers={"exact_match": ExactMatchScorer()},
            )
        )
    assert runner._gateway.requests == []


def test_identity_substitution_fails_closed() -> None:
    gateway = FakeGateway(provider_id="other-provider")
    evidence = asyncio.run(
        ModelBenchmarkRunner(gateway).run(
            run_id="identity-run",
            candidate=candidate(),
            suite=suite(),
            scorers={"exact_match": ExactMatchScorer()},
        )
    )
    assert evidence.complete is False
    assert evidence.attempts[0].status is AttemptStatus.FAILED
    assert evidence.attempts[0].error_code == "identity_mismatch:provider_id"
    with pytest.raises(ValueError, match="incomplete"):
        metric_observations(evidence, metrics=("quality.exact_match",))


def test_gateway_error_persists_only_typed_error_code() -> None:
    secret_message = "token=should-not-appear"
    gateway = FakeGateway(
        error=ModelGatewayError(
            ModelErrorCode.AUTHENTICATION,
            secret_message,
            provider_id="ollama",
        )
    )
    evidence = asyncio.run(
        ModelBenchmarkRunner(gateway).run(
            run_id="error-run",
            candidate=candidate(),
            suite=suite(),
            scorers={"exact_match": ExactMatchScorer()},
        )
    )
    document = evidence_document(evidence)
    assert evidence.complete is False
    assert "model_gateway:authentication" in document
    assert secret_message not in document


def test_scorer_cannot_spoof_reserved_infrastructure_metrics() -> None:
    bad_suite = BenchmarkSuite(
        suite_id="bad-metric",
        version="1",
        cases=(
            BenchmarkCase(
                case_id="case-1",
                messages=(ModelMessage(role="user", content="x"),),
                dataset_ref="dataset://x",
                dataset_version="1",
                scorer_id="bad_scorer",
            ),
        ),
    )
    with pytest.raises(ValueError, match="reserved"):
        asyncio.run(
            ModelBenchmarkRunner(FakeGateway()).run(
                run_id="bad-metric-run",
                candidate=candidate(),
                suite=bad_suite,
                scorers={"bad_scorer": ReservedMetricScorer()},
            )
        )


def test_experiment_adapter_aggregates_repetitions_without_promoting() -> None:
    benchmark_suite = suite(repetitions=2)
    evidence = asyncio.run(
        ModelBenchmarkRunner(FakeGateway()).run(
            run_id="experiment-run",
            candidate=candidate(),
            suite=benchmark_suite,
            scorers={"exact_match": ExactMatchScorer()},
        )
    )
    observations = metric_observations(
        evidence,
        metrics=("quality.exact_match", "model_lab.wall_latency_ms"),
    )
    assert [item.metric for item in observations] == [
        "quality.exact_match",
        "model_lab.wall_latency_ms",
    ]
    assert observations[0].value == 1.0

    challenger = ModelCandidate(
        candidate_id="cloud-challenger",
        provider_id="cloud",
        provider_kind=ProviderKind.CLOUD,
        model="cloud-model",
        model_version="2026-08-01",
        license_reference="license://cloud",
        provenance_reference="provider://cloud/model",
        permission_fingerprint="perm-v1",
    )
    definition = build_experiment_definition(
        experiment_id="model-lab-exp-1",
        champion=candidate(),
        challengers=(challenger,),
        suite=benchmark_suite,
        policy=PromotionPolicy(
            primary_metric="quality.exact_match",
            minimum_replays=1,
            guardrails=(
                MetricRule(
                    metric="model_lab.wall_latency_ms",
                    higher_is_better=False,
                    max_regression=20.0,
                ),
            ),
        ),
    )
    assert definition.champion.candidate_id == "ollama-qwen3-8b"
    assert definition.challengers[0].candidate_id == "cloud-challenger"
    assert definition.replays[0].replay_id == "arithmetic-1"
