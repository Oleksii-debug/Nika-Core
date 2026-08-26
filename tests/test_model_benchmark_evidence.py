from __future__ import annotations

import asyncio
import json
from dataclasses import asdict

import pytest

from nika_core.model_benchmarks import (
    BenchmarkCase,
    BenchmarkDataset,
    ModelBenchmarkCandidate,
    ModelBenchmarkRunner,
)
from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelResponse,
    ModelUsage,
    PrivacyClass,
    ProviderKind,
)
from nika_core.resources.contracts import ResourceSnapshot


class FakeCompletion:
    def __init__(self, provider_kind: ProviderKind = ProviderKind.LOCAL) -> None:
        self.requests = []
        self.provider_kind = provider_kind

    async def complete(self, request):
        self.requests.append(request)
        case_id = request.metadata["benchmark_case_id"]
        return ModelResponse(
            request_id=request.request_id,
            text=f"answer-{case_id}",
            provider_id=request.provider_id,
            provider_kind=self.provider_kind,
            model="resolved-model-v1",
            usage=ModelUsage(input_tokens=2, output_tokens=3, total_tokens=5),
        )


class SequenceResources:
    def __init__(self, values):
        self.values = list(values)

    def snapshot(self):
        return self.values.pop(0)


class SequenceClock:
    def __init__(self, values):
        self.values = list(values)

    def now(self):
        return self.values.pop(0)


class ExactScorer:
    def score(self, case, response_text):
        return 1.0 if response_text == f"answer-{case.case_id}" else 0.0


def dataset(secret: str = "PROMPT_SECRET") -> BenchmarkDataset:
    return BenchmarkDataset(
        dataset_id="core-smoke",
        version="2026-08-26-v1",
        cases=(
            BenchmarkCase("case-a", (ModelMessage("user", f"{secret}-a"),)),
            BenchmarkCase("case-b", (ModelMessage("user", f"{secret}-b"),)),
        ),
    )


def candidate(kind: ProviderKind = ProviderKind.LOCAL) -> ModelBenchmarkCandidate:
    return ModelBenchmarkCandidate(
        candidate_id="local-qwen",
        provider_id="ollama-local",
        provider_kind=kind,
        model="qwen3:8b",
        privacy=PrivacyClass.PRIVATE,
        timeout_seconds=10,
        expected_response_model="resolved-model-v1",
        license_reference="operator-reviewed:model-card",
        artifact_digest="sha256:abc123",
    )


def test_collects_versioned_redacted_quality_latency_and_resource_evidence():
    completion = FakeCompletion()
    resources = SequenceResources(
        [
            ResourceSnapshot(10.0, 20.0, 8_000),
            ResourceSnapshot(11.0, 21.0, 7_500),
            ResourceSnapshot(12.0, 22.0, 7_000),
            ResourceSnapshot(13.0, 23.0, 6_500),
        ]
    )
    runner = ModelBenchmarkRunner(
        completion=completion,
        resources=resources,
        scorer=ExactScorer(),
        clock=SequenceClock([1.0, 1.25, 2.0, 2.5]),
    )

    result = asyncio.run(runner.run(run_id="run-1", dataset=dataset(), candidate=candidate()))

    assert result.mean_quality_score == 1.0
    assert result.mean_latency_ms == 375.0
    assert [item.latency_ms for item in result.cases] == [250.0, 500.0]
    assert result.cases[0].resources.before.cpu_percent == 10.0
    assert result.cases[1].resources.after.available_memory_bytes == 6_500
    assert len(result.dataset_digest) == 64
    assert all(len(item.case_digest) == 64 for item in result.cases)
    assert all(len(item.response_digest) == 64 for item in result.cases)

    serialized = json.dumps(asdict(result), default=str, sort_keys=True)
    assert "PROMPT_SECRET" not in serialized
    assert "answer-case" not in serialized
    assert "operator-reviewed:model-card" in serialized

    assert len(completion.requests) == 2
    for request in completion.requests:
        assert request.provider_id == "ollama-local"
        assert request.model == "qwen3:8b"
        assert request.fallback_provider_ids == ()
        assert request.privacy is PrivacyClass.PRIVATE
        assert set(request.metadata) == {
            "purpose",
            "benchmark_run_id",
            "benchmark_dataset_id",
            "benchmark_dataset_version",
            "benchmark_case_id",
            "benchmark_candidate_id",
        }


@pytest.mark.parametrize("kind", list(ProviderKind))
def test_completion_port_supports_all_provider_kinds_without_routing_authority(kind):
    completion = FakeCompletion(provider_kind=kind)
    one_case = BenchmarkDataset(
        dataset_id="kinds",
        version="v1",
        cases=(BenchmarkCase("one", (ModelMessage("user", "hello"),)),),
    )
    runner = ModelBenchmarkRunner(
        completion=completion,
        resources=SequenceResources(
            [ResourceSnapshot(1.0, 2.0, 3), ResourceSnapshot(1.0, 2.0, 3)]
        ),
        scorer=ExactScorer(),
        clock=SequenceClock([1.0, 1.1]),
    )

    result = asyncio.run(
        runner.run(run_id=f"run-{kind.value}", dataset=one_case, candidate=candidate(kind))
    )

    assert result.provider_kind is kind
    assert result.cases[0].provider_kind is kind


def test_dataset_and_case_hashes_change_when_prompt_changes():
    def run(ds):
        runner = ModelBenchmarkRunner(
            completion=FakeCompletion(),
            resources=SequenceResources(
                [
                    ResourceSnapshot(1.0, 2.0, 3),
                    ResourceSnapshot(1.0, 2.0, 3),
                    ResourceSnapshot(1.0, 2.0, 3),
                    ResourceSnapshot(1.0, 2.0, 3),
                ]
            ),
            scorer=ExactScorer(),
            clock=SequenceClock([1.0, 1.1, 2.0, 2.1]),
        )
        return asyncio.run(
            runner.run(run_id="same-run", dataset=ds, candidate=candidate())
        )

    first = run(dataset("first-secret"))
    second = run(dataset("second-secret"))

    assert first.dataset_digest != second.dataset_digest
    assert first.cases[0].case_digest != second.cases[0].case_digest


def test_rejects_response_identity_drift():
    class WrongProvider(FakeCompletion):
        async def complete(self, request):
            response = await super().complete(request)
            return ModelResponse(
                request_id=response.request_id,
                text=response.text,
                provider_id="other-provider",
                provider_kind=response.provider_kind,
                model=response.model,
            )

    runner = ModelBenchmarkRunner(
        completion=WrongProvider(),
        resources=SequenceResources(
            [ResourceSnapshot(1.0, 2.0, 3), ResourceSnapshot(1.0, 2.0, 3)]
        ),
        scorer=ExactScorer(),
        clock=SequenceClock([1.0, 1.1]),
    )
    one_case = BenchmarkDataset(
        dataset_id="identity",
        version="v1",
        cases=(BenchmarkCase("one", (ModelMessage("user", "hello"),)),),
    )

    with pytest.raises(ValueError, match="provider_id mismatch"):
        asyncio.run(runner.run(run_id="run", dataset=one_case, candidate=candidate()))


@pytest.mark.parametrize("score", [True, float("nan"), -0.01, 1.01, "1.0"])
def test_rejects_malformed_or_out_of_range_quality_score(score):
    class BadScorer:
        def score(self, case, response_text):
            return score

    runner = ModelBenchmarkRunner(
        completion=FakeCompletion(),
        resources=SequenceResources(
            [ResourceSnapshot(1.0, 2.0, 3), ResourceSnapshot(1.0, 2.0, 3)]
        ),
        scorer=BadScorer(),
        clock=SequenceClock([1.0, 1.1]),
    )
    one_case = BenchmarkDataset(
        dataset_id="quality",
        version="v1",
        cases=(BenchmarkCase("one", (ModelMessage("user", "hello"),)),),
    )

    with pytest.raises(ValueError, match="scorer"):
        asyncio.run(runner.run(run_id="run", dataset=one_case, candidate=candidate()))


@pytest.mark.parametrize(
    "snapshot",
    [
        ResourceSnapshot(float("nan"), 2.0, 3),
        ResourceSnapshot(1.0, 101.0, 3),
        ResourceSnapshot(1.0, 2.0, -1),
        ResourceSnapshot(True, 2.0, 3),
    ],
)
def test_rejects_malformed_resource_evidence_before_model_effect(snapshot):
    completion = FakeCompletion()
    runner = ModelBenchmarkRunner(
        completion=completion,
        resources=SequenceResources([snapshot]),
        scorer=ExactScorer(),
        clock=SequenceClock([1.0, 1.1]),
    )
    one_case = BenchmarkDataset(
        dataset_id="resource",
        version="v1",
        cases=(BenchmarkCase("one", (ModelMessage("user", "hello"),)),),
    )

    with pytest.raises(ValueError):
        asyncio.run(runner.run(run_id="run", dataset=one_case, candidate=candidate()))
    assert completion.requests == []


def test_rejects_clock_regression_after_effect_instead_of_claiming_latency_evidence():
    runner = ModelBenchmarkRunner(
        completion=FakeCompletion(),
        resources=SequenceResources(
            [ResourceSnapshot(1.0, 2.0, 3), ResourceSnapshot(1.0, 2.0, 3)]
        ),
        scorer=ExactScorer(),
        clock=SequenceClock([2.0, 1.0]),
    )
    one_case = BenchmarkDataset(
        dataset_id="clock",
        version="v1",
        cases=(BenchmarkCase("one", (ModelMessage("user", "hello"),)),),
    )

    with pytest.raises(ValueError, match="moved backwards"):
        asyncio.run(runner.run(run_id="run", dataset=one_case, candidate=candidate()))


def test_contracts_reject_duplicate_cases_and_invalid_candidate_timeout():
    case = BenchmarkCase("same", (ModelMessage("user", "hello"),))
    with pytest.raises(ValueError, match="unique"):
        BenchmarkDataset("dataset", "v1", (case, case))
    with pytest.raises(ValueError, match="timeout_seconds"):
        ModelBenchmarkCandidate(
            candidate_id="candidate",
            provider_id="provider",
            provider_kind=ProviderKind.LOCAL,
            model="model",
            timeout_seconds=True,
        )
