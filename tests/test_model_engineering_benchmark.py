from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from nika_core.model_engineering import (
    AcceleratorSnapshot,
    EvaluationCase,
    EvaluationPurpose,
    EvaluationSet,
    ModelBenchmarkError,
    ModelBenchmarkIdentityError,
    ModelBenchmarkRunner,
    ModelCandidate,
    benchmark_report_json,
    benchmark_report_sha256,
    render_text_report,
)
from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelGatewayError,
    ModelMessage,
    ModelResponse,
    ModelUsage,
    PrivacyClass,
    ProviderKind,
)
from nika_core.resources.contracts import ResourceSnapshot


def _candidate(
    *,
    candidate_id: str = "qwen-local",
    request_model: str = "qwen3:8b",
    response_model: str = "qwen3:8b",
) -> ModelCandidate:
    return ModelCandidate(
        candidate_id=candidate_id,
        provider_id="ollama-local",
        provider_kind=ProviderKind.LOCAL,
        request_model=request_model,
        expected_response_model=response_model,
        engine_provenance_ref="pkg:ollama-adapter@1",
        engine_license_ref="license:adapter",
        model_provenance_ref=f"ollama:{response_model}",
        model_license_ref="license:qwen-model",
    )


def _evaluation_set() -> EvaluationSet:
    return EvaluationSet(
        evaluation_set_id="ua-core-smoke",
        version="2026-08-26.v1",
        provenance_ref="dataset:ua-core-smoke",
        license_ref="license:internal-eval",
        purpose=EvaluationPurpose.HELD_OUT,
        privacy=PrivacyClass.PRIVATE,
        cases=(
            EvaluationCase(
                case_id="exact",
                messages=(ModelMessage("user", "secret prompt one"),),
                expected_text="очікувана відповідь",
                weight=1.0,
            ),
            EvaluationCase(
                case_id="provider-failure",
                messages=(ModelMessage("user", "secret prompt two"),),
                expected_text="not persisted in report",
                weight=3.0,
            ),
        ),
    )


class _FakeGateway:
    async def complete(self, request):
        if request.metadata["evaluation_case_id"] == "provider-failure":
            raise ModelGatewayError(
                ModelErrorCode.UNAVAILABLE,
                "synthetic provider failure with secret detail",
                provider_id=request.provider_id,
            )
        return ModelResponse(
            request_id=request.request_id,
            text="очікувана відповідь",
            provider_id="ollama-local",
            provider_kind=ProviderKind.LOCAL,
            model="qwen3:8b",
            usage=ModelUsage(input_tokens=4, output_tokens=2, total_tokens=6),
            latency_ms=999.0,
        )


class _IdentityMismatchGateway:
    async def complete(self, request):
        return ModelResponse(
            request_id=request.request_id,
            text="answer",
            provider_id="wrong-provider",
            provider_kind=ProviderKind.LOCAL,
            model="qwen3:8b",
        )


class _BadUsageGateway:
    async def complete(self, request):
        return ModelResponse(
            request_id=request.request_id,
            text="answer",
            provider_id="ollama-local",
            provider_kind=ProviderKind.LOCAL,
            model="qwen3:8b",
            usage=ModelUsage(input_tokens=True, output_tokens=1, total_tokens=2),
        )


class _SnapshotObserver:
    def __init__(self, snapshots):
        self._snapshots = iter(snapshots)

    def snapshot(self):
        return next(self._snapshots)


class _AcceleratorObserver:
    def __init__(self, snapshots):
        self._snapshots = iter(snapshots)

    def snapshot(self):
        return next(self._snapshots)


class _Clock:
    def __init__(self, values):
        self._values = iter(values)

    def __call__(self):
        return next(self._values)


def test_evaluation_set_hash_binds_content_version_and_expected_answer() -> None:
    original = _evaluation_set()
    changed = EvaluationSet(
        evaluation_set_id=original.evaluation_set_id,
        version=original.version,
        provenance_ref=original.provenance_ref,
        license_ref=original.license_ref,
        purpose=original.purpose,
        privacy=original.privacy,
        cases=(
            EvaluationCase(
                case_id="exact",
                messages=(ModelMessage("user", "changed prompt"),),
                expected_text="очікувана відповідь",
            ),
            original.cases[1],
        ),
    )

    assert len(original.content_sha256) == 64
    assert original.content_sha256 != changed.content_sha256


def test_candidate_keeps_engine_and_model_license_evidence_separate() -> None:
    candidate = _candidate()

    assert candidate.engine_license_ref == "license:adapter"
    assert candidate.model_license_ref == "license:qwen-model"
    assert candidate.engine_license_ref != candidate.model_license_ref
    assert len(candidate.evidence_sha256) == 64

    with pytest.raises(ValueError, match="model_license_ref"):
        ModelCandidate(
            candidate_id="bad",
            provider_id="local",
            provider_kind=ProviderKind.LOCAL,
            request_model="m",
            expected_response_model="m",
            engine_provenance_ref="engine",
            engine_license_ref="engine-license",
            model_provenance_ref="model",
            model_license_ref="",
        )


def test_benchmark_records_quality_failures_resources_without_raw_text() -> None:
    resources = (
        ResourceSnapshot(10.0, 20.0, 8_000),
        ResourceSnapshot(30.0, 40.0, 7_000),
        ResourceSnapshot(50.0, 35.0, 6_000),
        ResourceSnapshot(20.0, 45.0, 5_000),
    )
    accelerator = (
        AcceleratorSnapshot(5.0, 100),
        AcceleratorSnapshot(25.0, 200),
        AcceleratorSnapshot(10.0, 150),
        AcceleratorSnapshot(60.0, 500),
    )
    runner = ModelBenchmarkRunner(
        _FakeGateway(),
        resource_observer=_SnapshotObserver(resources),
        accelerator_observer=_AcceleratorObserver(accelerator),
        clock=_Clock((1.0, 1.1, 2.0, 2.2)),
    )

    report = asyncio.run(runner.benchmark(_candidate(), _evaluation_set()))

    assert report.weighted_quality_score == pytest.approx(0.25)
    assert report.task_pass_rate == pytest.approx(0.5)
    assert report.completion_rate == pytest.approx(0.5)
    assert report.mean_latency_ms == pytest.approx(150.0)
    assert report.p95_latency_ms == pytest.approx(200.0)
    assert report.peak_cpu_percent == 50.0
    assert report.peak_memory_percent == 45.0
    assert report.min_available_memory_bytes == 5_000
    assert report.peak_accelerator_percent == 60.0
    assert report.peak_accelerator_memory_bytes == 500

    first, second = report.case_results
    assert first.response_sha256 == hashlib.sha256(
        "очікувана відповідь".encode()
    ).hexdigest()
    assert second.error_code is ModelErrorCode.UNAVAILABLE
    assert second.response_sha256 is None

    machine = benchmark_report_json(report)
    accessible = render_text_report(report)
    json.loads(machine)
    for secret in (
        "secret prompt one",
        "secret prompt two",
        "очікувана відповідь",
        "not persisted in report",
        "synthetic provider failure with secret detail",
    ):
        assert secret not in machine
        assert secret not in accessible
    assert len(benchmark_report_sha256(report)) == 64
    assert "Cases:" in accessible
    assert "provider-failure: FAIL" in accessible


def test_benchmark_fails_closed_on_response_identity_mismatch() -> None:
    evaluation = EvaluationSet(
        evaluation_set_id="one",
        version="1",
        provenance_ref="dataset:one",
        license_ref="license:one",
        purpose=EvaluationPurpose.DEVELOPMENT,
        privacy=PrivacyClass.PUBLIC,
        cases=(
            EvaluationCase(
                case_id="case",
                messages=(ModelMessage("user", "prompt"),),
                expected_text="answer",
            ),
        ),
    )
    runner = ModelBenchmarkRunner(
        _IdentityMismatchGateway(),
        clock=_Clock((1.0, 1.1)),
    )

    with pytest.raises(ModelBenchmarkIdentityError, match="provider identity"):
        asyncio.run(runner.benchmark(_candidate(), evaluation))


def test_benchmark_rejects_malformed_usage_instead_of_coercing_bool() -> None:
    evaluation = EvaluationSet(
        evaluation_set_id="one",
        version="1",
        provenance_ref="dataset:one",
        license_ref="license:one",
        purpose=EvaluationPurpose.DEVELOPMENT,
        privacy=PrivacyClass.PUBLIC,
        cases=(
            EvaluationCase(
                case_id="case",
                messages=(ModelMessage("user", "prompt"),),
                expected_text="answer",
            ),
        ),
    )
    runner = ModelBenchmarkRunner(
        _BadUsageGateway(),
        clock=_Clock((1.0, 1.1)),
    )

    with pytest.raises(ModelBenchmarkError, match="non-negative integer"):
        asyncio.run(runner.benchmark(_candidate(), evaluation))


def test_benchmark_suite_is_sequential_and_rejects_duplicate_candidate_ids() -> None:
    evaluation = EvaluationSet(
        evaluation_set_id="one",
        version="1",
        provenance_ref="dataset:one",
        license_ref="license:one",
        purpose=EvaluationPurpose.DEVELOPMENT,
        privacy=PrivacyClass.PUBLIC,
        cases=(
            EvaluationCase(
                case_id="case",
                messages=(ModelMessage("user", "prompt"),),
                expected_text="answer",
            ),
        ),
    )
    candidate = _candidate()

    with pytest.raises(ValueError, match="candidate IDs must be unique"):
        asyncio.run(
            ModelBenchmarkRunner(_FakeGateway()).benchmark_suite(
                (candidate, candidate),
                evaluation,
            )
        )
