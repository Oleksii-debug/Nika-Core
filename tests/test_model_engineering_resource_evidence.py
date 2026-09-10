from __future__ import annotations

import asyncio
import json

import pytest

from nika_core.model_engineering import (
    AcceleratorSnapshot,
    EvaluationCase,
    EvaluationPurpose,
    EvaluationSet,
    ModelBenchmarkRunner,
    ModelCandidate,
    benchmark_report_json,
)
from nika_core.model_engineering.resource_evidence import (
    ResourceMeasurementScope,
    ResourceMeasurementStatus,
    ResourceMeasurementUnit,
    ResourceMetricEvidence,
    ResourceUnknownReason,
)
from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelResponse,
    PrivacyClass,
    ProviderKind,
)
from nika_core.resources.contracts import ResourceSnapshot


class _Gateway:
    async def complete(self, request):
        return ModelResponse(
            request_id=request.request_id,
            text="ok",
            provider_id="fake-local",
            provider_kind=ProviderKind.LOCAL,
            model="fake-model",
        )


class _FakeResourceObserver:
    def __init__(self, snapshots):
        self._snapshots = iter(snapshots)
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        return next(self._snapshots)


class _FakeAcceleratorObserver:
    def __init__(self, snapshots):
        self._snapshots = iter(snapshots)
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        return next(self._snapshots)


class _Clock:
    def __init__(self, values):
        self._values = iter(values)

    def __call__(self):
        return next(self._values)


def _candidate() -> ModelCandidate:
    return ModelCandidate(
        candidate_id="fake-candidate",
        provider_id="fake-local",
        provider_kind=ProviderKind.LOCAL,
        request_model="fake-model",
        expected_response_model="fake-model",
        engine_provenance_ref="engine:test",
        engine_license_ref="license:test-engine",
        model_provenance_ref="model:test",
        model_license_ref="license:test-model",
    )


def _evaluation() -> EvaluationSet:
    return EvaluationSet(
        evaluation_set_id="resource-evidence",
        version="1",
        provenance_ref="dataset:test",
        license_ref="license:test-data",
        purpose=EvaluationPurpose.DEVELOPMENT,
        privacy=PrivacyClass.PUBLIC,
        cases=(
            EvaluationCase(
                case_id="case",
                messages=(ModelMessage("user", "prompt"),),
                expected_text="ok",
            ),
        ),
    )


def test_fake_resource_observer_emits_explicit_unknown_without_erasing_zero() -> None:
    resources = _FakeResourceObserver(
        (
            ResourceSnapshot(
                cpu_percent=12.5,
                memory_percent=25.0,
                available_memory_bytes=8_000,
                process_rss_bytes=None,
            ),
            ResourceSnapshot(
                cpu_percent=42.0,
                memory_percent=40.0,
                available_memory_bytes=6_000,
                process_rss_bytes=0,
            ),
        )
    )
    accelerator = _FakeAcceleratorObserver(
        (
            AcceleratorSnapshot(utilization_percent=91.0, memory_used_bytes=2_048),
            AcceleratorSnapshot(utilization_percent=50.0, memory_used_bytes=1_024),
        )
    )
    runner = ModelBenchmarkRunner(
        _Gateway(),
        resource_observer=resources,
        accelerator_observer=accelerator,
        clock=_Clock((10.0, 10.1)),
    )

    report = asyncio.run(runner.benchmark(_candidate(), _evaluation()))
    payload = json.loads(benchmark_report_json(report))["resource_evidence"]

    assert resources.calls == 2
    assert accelerator.calls == 2
    assert payload["sampling"] == {
        "mode": "bounded_point_snapshots",
        "capture_points": ["before", "after"],
        "during": "not_captured",
    }

    before, after = payload["samples"]
    assert before["phase"] == "before"
    assert before["host_cpu_percent"] == {
        "status": "observed",
        "scope": "host",
        "unit": "percent",
        "value": 12.5,
        "unknown_reason": None,
    }
    assert before["nika_process_rss_bytes"]["status"] == "unknown"
    assert before["nika_process_rss_bytes"]["value"] is None
    assert (
        before["nika_process_rss_bytes"]["unknown_reason"]
        == "metric_unavailable"
    )

    assert after["phase"] == "after"
    assert after["nika_process_rss_bytes"]["status"] == "observed"
    assert after["nika_process_rss_bytes"]["value"] == 0
    assert after["nika_process_rss_bytes"]["unknown_reason"] is None

    # Existing accelerator evidence is intentionally generic. Without accelerator
    # kind attestation, even a numeric 91% reading is not truthful GPU evidence.
    assert report.peak_accelerator_percent == 91.0
    assert before["untyped_accelerator_observation_present"] is True
    assert before["gpu_utilization_percent"]["status"] == "unknown"
    assert before["gpu_utilization_percent"]["value"] is None
    assert (
        before["gpu_utilization_percent"]["unknown_reason"]
        == "gpu_identity_unavailable"
    )
    assert payload["summary"]["peak_gpu_utilization_percent"]["value"] is None


def test_missing_resource_observer_is_unknown_not_zero() -> None:
    runner = ModelBenchmarkRunner(
        _Gateway(),
        clock=_Clock((1.0, 1.25)),
    )

    report = asyncio.run(runner.benchmark(_candidate(), _evaluation()))
    payload = json.loads(benchmark_report_json(report))["resource_evidence"]

    for sample in payload["samples"]:
        metric = sample["host_ram_percent"]
        assert metric["status"] == "unknown"
        assert metric["value"] is None
        assert metric["unknown_reason"] == "resource_observer_not_configured"

    summary = payload["summary"]["peak_host_cpu_percent"]
    assert summary["status"] == "unknown"
    assert summary["value"] is None
    assert summary["unknown_reason"] == "no_observed_sample"


def test_resource_metric_contract_rejects_unknown_zero_and_invalid_percent() -> None:
    with pytest.raises(ValueError, match="unknown resource metric"):
        ResourceMetricEvidence(
            status=ResourceMeasurementStatus.UNKNOWN,
            scope=ResourceMeasurementScope.HOST,
            unit=ResourceMeasurementUnit.BYTES,
            value=0,
            unknown_reason=ResourceUnknownReason.METRIC_UNAVAILABLE,
        )

    with pytest.raises(ValueError, match=r"\[0, 100\]"):
        ResourceMetricEvidence(
            status=ResourceMeasurementStatus.OBSERVED,
            scope=ResourceMeasurementScope.HOST,
            unit=ResourceMeasurementUnit.PERCENT,
            value=101.0,
        )
