"""QA_ONLY / DO_NOT_MERGE: deterministic latency-contract oracles for PR #534.

These tests intentionally target the exact production parent
c91d95e076ae7593c39d22c6e0ebf937b139773e. They use only injected fake
clocks and fake gateways. No wall-clock timing or hardware benchmark claim is
made here.
"""

from __future__ import annotations

import asyncio
from math import nan

import pytest

import nika_core.model_engineering.runner as runner_module
from nika_core.model_engineering.contracts import (
    EvaluationCase,
    EvaluationPurpose,
    EvaluationSet,
    ModelCandidate,
)
from nika_core.model_engineering.runner import ModelBenchmarkError, ModelBenchmarkRunner
from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelGatewayError,
    ModelMessage,
    ModelResponse,
    ModelUsage,
    PrivacyClass,
    ProviderKind,
)


class _Clock:
    def __init__(self, values: tuple[float, ...], events: list[str] | None = None) -> None:
        self._values = iter(values)
        self._events = events
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        if self._events is not None:
            self._events.append("clock_start" if self.calls == 1 else "clock_finish")
        return next(self._values)


class _SuccessGateway:
    def __init__(self, events: list[str] | None = None) -> None:
        self._events = events

    async def complete(self, request):
        if self._events is not None:
            self._events.append("gateway")
        return ModelResponse(
            request_id=request.request_id,
            text="answer",
            provider_id="local-test",
            provider_kind=ProviderKind.LOCAL,
            model="model-a",
            usage=ModelUsage(input_tokens=2, output_tokens=1, total_tokens=3),
            latency_ms=999_999.0,
        )


class _MixedGateway:
    def __init__(self, *, failure_code: ModelErrorCode) -> None:
        self._failure_code = failure_code

    async def complete(self, request):
        if request.metadata["evaluation_case_id"] == "failure":
            raise ModelGatewayError(
                self._failure_code,
                "synthetic failure",
                provider_id="local-test",
                retryable=False,
            )
        return ModelResponse(
            request_id=request.request_id,
            text="answer",
            provider_id="local-test",
            provider_kind=ProviderKind.LOCAL,
            model="model-a",
            usage=ModelUsage(input_tokens=2, output_tokens=1, total_tokens=3),
        )


class _CancelledGateway:
    async def complete(self, request):
        del request
        raise asyncio.CancelledError()


def _candidate() -> ModelCandidate:
    return ModelCandidate(
        candidate_id="candidate-a",
        provider_id="local-test",
        provider_kind=ProviderKind.LOCAL,
        request_model="model-a",
        expected_response_model="model-a",
        engine_provenance_ref="engine:test",
        engine_license_ref="license:engine-test",
        model_provenance_ref="model:test-a",
        model_license_ref="license:model-test",
    )


def _evaluation(*case_ids: str) -> EvaluationSet:
    return EvaluationSet(
        evaluation_set_id="latency-contract",
        version="1",
        provenance_ref="dataset:latency-contract",
        license_ref="license:internal-test",
        purpose=EvaluationPurpose.DEVELOPMENT,
        privacy=PrivacyClass.PUBLIC,
        cases=tuple(
            EvaluationCase(
                case_id=case_id,
                messages=(ModelMessage("user", f"prompt-{case_id}"),),
                expected_text="answer",
            )
            for case_id in case_ids
        ),
    )


def test_latency_boundary_is_gateway_start_through_validated_response(monkeypatch) -> None:
    """Canonical success latency excludes setup but includes Nika response validation."""

    events: list[str] = []
    real_request = runner_module.ModelRequest
    real_validate = ModelBenchmarkRunner._validate_response_identity
    real_usage = ModelBenchmarkRunner._usage

    def request_spy(*args, **kwargs):
        events.append("request")
        return real_request(*args, **kwargs)

    def validate_spy(candidate, request, response) -> None:
        events.append("validate")
        real_validate(candidate, request, response)

    def usage_spy(response):
        events.append("usage")
        return real_usage(response)

    monkeypatch.setattr(runner_module, "ModelRequest", request_spy)
    monkeypatch.setattr(
        ModelBenchmarkRunner,
        "_validate_response_identity",
        staticmethod(validate_spy),
    )
    monkeypatch.setattr(ModelBenchmarkRunner, "_usage", staticmethod(usage_spy))

    runner = ModelBenchmarkRunner(
        _SuccessGateway(events),
        clock=_Clock((10.0, 10.125), events),
    )

    report = asyncio.run(runner.benchmark(_candidate(), _evaluation("success")))

    assert report.case_results[0].latency_ms == pytest.approx(125.0)
    assert events == [
        "request",
        "clock_start",
        "gateway",
        "validate",
        "usage",
        "clock_finish",
    ]


@pytest.mark.parametrize(
    "failure_code",
    [ModelErrorCode.UNAVAILABLE, ModelErrorCode.CANCELLED],
)
def test_failed_or_cancelled_attempt_duration_does_not_pollute_success_latency(
    failure_code: ModelErrorCode,
) -> None:
    """Mean/P95 latency describe validated successful completions only."""

    runner = ModelBenchmarkRunner(
        _MixedGateway(failure_code=failure_code),
        # success = 100 ms, failed/cancelled attempt = 900 ms
        clock=_Clock((1.0, 1.1, 2.0, 2.9)),
    )

    report = asyncio.run(
        runner.benchmark(_candidate(), _evaluation("success", "failure"))
    )

    success, failure = report.case_results
    assert success.completion_succeeded is True
    assert success.latency_ms == pytest.approx(100.0)
    assert failure.completion_succeeded is False
    assert failure.error_code is failure_code
    assert failure.latency_ms == pytest.approx(900.0)
    assert report.mean_latency_ms == pytest.approx(100.0)
    assert report.p95_latency_ms == pytest.approx(100.0)


@pytest.mark.parametrize("clock_values", [(2.0, 1.0), (1.0, nan)])
def test_non_monotonic_or_non_finite_clock_fails_closed(
    clock_values: tuple[float, float],
) -> None:
    runner = ModelBenchmarkRunner(_SuccessGateway(), clock=_Clock(clock_values))

    with pytest.raises(ModelBenchmarkError, match="clock moved backwards|non-finite"):
        asyncio.run(runner.benchmark(_candidate(), _evaluation("success")))


def test_async_cancellation_aborts_without_manufacturing_latency_sample() -> None:
    clock = _Clock((5.0,))
    runner = ModelBenchmarkRunner(_CancelledGateway(), clock=clock)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runner.benchmark(_candidate(), _evaluation("cancelled")))

    assert clock.calls == 1
