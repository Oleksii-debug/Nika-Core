from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace

import pytest

from nika_core.model_engineering import (
    AcceleratorSnapshot,
    BenchmarkExecutionConfig,
    CaseBenchmarkResult,
    EvaluationCase,
    EvaluationPurpose,
    EvaluationSet,
    ModelBenchmarkError,
    ModelBenchmarkIdentityError,
    ModelBenchmarkRunner,
    ModelCandidate,
    benchmark_accessible_report_json,
    benchmark_configuration_sha256,
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


class _CountingGateway:
    def __init__(self) -> None:
        self.calls = 0
        self._delegate = _FakeGateway()

    async def complete(self, request):
        self.calls += 1
        return await self._delegate.complete(request)


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

    assert report.execution_config_sha256 == BenchmarkExecutionConfig().evidence_sha256
    assert report.weighted_quality_score == pytest.approx(0.25)
    assert report.task_pass_rate == pytest.approx(0.5)
    assert report.completion_rate == pytest.approx(0.5)
    assert report.mean_latency_ms == pytest.approx(100.0)
    assert report.p95_latency_ms == pytest.approx(100.0)
    assert report.peak_cpu_percent == 50.0
    assert report.peak_memory_percent == 45.0
    assert report.min_available_memory_bytes == 5_000
    assert report.peak_accelerator_percent == 60.0
    assert report.peak_accelerator_memory_bytes == 500

    first, second = report.case_results
    assert first.evaluation_weight == 1.0
    assert second.evaluation_weight == 3.0
    assert first.response_sha256 == hashlib.sha256(
        "очікувана відповідь".encode()
    ).hexdigest()
    assert second.error_code is ModelErrorCode.UNAVAILABLE
    assert second.response_sha256 is None

    machine = benchmark_report_json(report)
    accessible_machine = benchmark_accessible_report_json(report)
    machine_payload = json.loads(machine)
    assert [item["evaluation_weight"] for item in machine_payload["cases"]] == [
        1.0,
        3.0,
    ]
    substitutions = {
        "weighted_quality_score": 0.5,
        "task_pass_rate": 0.75,
        "completion_rate": 0.75,
        "mean_latency_ms": 101.0,
        "p95_latency_ms": 101.0,
        "peak_cpu_percent": 49.0,
        "peak_memory_percent": 44.0,
        "min_available_memory_bytes": 5_001,
        "peak_accelerator_percent": 59.0,
        "peak_accelerator_memory_bytes": 499,
    }
    for field, value in substitutions.items():
        forged = replace(report, **{field: value})
        for serializer in (
            benchmark_report_json,
            benchmark_accessible_report_json,
        ):
            with pytest.raises(ValueError, match="aggregate metrics"):
                serializer(forged)

    accessible = render_text_report(report)
    json.loads(machine)
    accessible_payload = json.loads(accessible_machine)
    for secret in (
        "secret prompt one",
        "secret prompt two",
        "очікувана відповідь",
        "not persisted in report",
        "synthetic provider failure with secret detail",
    ):
        assert secret not in machine
        assert secret not in accessible_machine
        assert secret not in accessible
    assert len(benchmark_report_sha256(report)) == 64
    assert report.execution_config_sha256 in machine
    assert list(accessible_payload) == [
        "schema",
        "model",
        "provider",
        "dataset",
        "quality",
        "latency",
        "resources",
        "failures",
        "recommendation",
        "evidence_limitations",
    ]
    assert accessible_payload["recommendation"] == {
        "status": "not_evaluated",
        "reason": "benchmark_evidence_only",
    }
    assert accessible_payload["failures"] == [
        {"case_id": "provider-failure", "error_code": "unavailable"}
    ]
    headings = [
        "Model",
        "Provider",
        "Dataset",
        "Quality",
        "Latency",
        "Resources",
        "Failures",
        "Recommendation",
        "Evidence limitations",
    ]
    positions = [accessible.splitlines().index(heading) for heading in headings]
    assert positions == sorted(positions)
    assert accessible == render_text_report(report)
    assert accessible_machine == benchmark_accessible_report_json(report)
    assert "human_nvda_verification_not_attested" in accessible
    assert "NVDA_VERIFIED=true" not in accessible
    assert f"Execution config SHA-256: {report.execution_config_sha256}" in accessible
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


def test_evaluation_set_identity_is_stable_and_order_bound() -> None:
    original = _evaluation_set()
    equivalent = _evaluation_set()
    reversed_cases = EvaluationSet(
        evaluation_set_id=original.evaluation_set_id,
        version=original.version,
        provenance_ref=original.provenance_ref,
        license_ref=original.license_ref,
        purpose=original.purpose,
        privacy=original.privacy,
        cases=tuple(reversed(original.cases)),
    )

    assert equivalent.content_sha256 == original.content_sha256
    assert reversed_cases.content_sha256 != original.content_sha256


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("version", "2026-08-26.v2"),
        ("provenance_ref", "dataset:changed"),
        ("license_ref", "license:changed"),
        ("purpose", EvaluationPurpose.DEVELOPMENT),
        ("privacy", PrivacyClass.PUBLIC),
    ),
)
def test_evaluation_set_identity_changes_for_bound_set_field(field, value) -> None:
    original = _evaluation_set()
    kwargs = {
        "evaluation_set_id": original.evaluation_set_id,
        "version": original.version,
        "provenance_ref": original.provenance_ref,
        "license_ref": original.license_ref,
        "purpose": original.purpose,
        "privacy": original.privacy,
        "cases": original.cases,
    }
    kwargs[field] = value
    changed = EvaluationSet(**kwargs)

    assert changed.content_sha256 != original.content_sha256


def test_evaluation_set_identity_binds_expected_score_and_weight() -> None:
    original = _evaluation_set()
    case = original.cases[0]
    variants = (
        EvaluationCase(
            case_id=case.case_id,
            messages=case.messages,
            expected_text="different expected",
            pass_score=case.pass_score,
            weight=case.weight,
        ),
        EvaluationCase(
            case_id=case.case_id,
            messages=case.messages,
            expected_text=case.expected_text,
            pass_score=0.5,
            weight=case.weight,
        ),
        EvaluationCase(
            case_id=case.case_id,
            messages=case.messages,
            expected_text=case.expected_text,
            pass_score=case.pass_score,
            weight=2.0,
        ),
    )
    for variant in variants:
        changed = EvaluationSet(
            evaluation_set_id=original.evaluation_set_id,
            version=original.version,
            provenance_ref=original.provenance_ref,
            license_ref=original.license_ref,
            purpose=original.purpose,
            privacy=original.privacy,
            cases=(variant, original.cases[1]),
        )
        assert changed.content_sha256 != original.content_sha256


def test_evaluation_set_rejects_duplicate_case_ids() -> None:
    original = _evaluation_set()
    duplicate = EvaluationCase(
        case_id=original.cases[0].case_id,
        messages=(ModelMessage("user", "other"),),
        expected_text="other",
    )

    with pytest.raises(ValueError, match="IDs must be unique"):
        EvaluationSet(
            evaluation_set_id=original.evaluation_set_id,
            version=original.version,
            provenance_ref=original.provenance_ref,
            license_ref=original.license_ref,
            purpose=original.purpose,
            privacy=original.privacy,
            cases=(original.cases[0], duplicate),
        )


class _ProviderKindSpyGateway:
    def __init__(self) -> None:
        self.seen_kind = None

    async def complete(self, request):
        self.seen_kind = request.provider_kind
        return ModelResponse(
            request_id=request.request_id,
            text="answer",
            provider_id=request.provider_id,
            provider_kind=request.provider_kind,
            model=request.model,
        )


def test_benchmark_binds_candidate_provider_kind_before_dispatch() -> None:
    gateway = _ProviderKindSpyGateway()
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

    asyncio.run(ModelBenchmarkRunner(gateway, clock=_Clock((1.0, 1.1))).benchmark(
        _candidate(), evaluation
    ))

    assert gateway.seen_kind is ProviderKind.LOCAL


class _AlwaysFailGateway:
    async def complete(self, request):
        raise ModelGatewayError(
            ModelErrorCode.UNAVAILABLE,
            "synthetic",
            provider_id=request.provider_id,
        )


def test_latency_aggregates_exclude_failures_and_report_unavailable_without_success() -> None:
    runner = ModelBenchmarkRunner(_AlwaysFailGateway(), clock=_Clock((1.0, 1.2, 2.0, 2.4)))

    report = asyncio.run(runner.benchmark(_candidate(), _evaluation_set()))

    assert [item.latency_ms for item in report.case_results] == pytest.approx([200.0, 400.0])
    assert report.mean_latency_ms is None
    assert report.p95_latency_ms is None
    assert "Mean latency ms: not measured" in render_text_report(report)
    assert '"mean_latency_ms":null' in benchmark_report_json(report)


def test_latency_clock_validation_happens_after_response_identity_and_usage() -> None:
    runner = ModelBenchmarkRunner(_BadUsageGateway(), clock=_Clock((2.0, 1.0)))
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

    with pytest.raises(ModelBenchmarkError, match="non-negative integer"):
        asyncio.run(runner.benchmark(_candidate(), evaluation))


class _HostileText(str):
    def strip(self, chars=None):
        raise AssertionError("hostile text behavior executed")


class _HostileFloat(float):
    def __float__(self):
        raise AssertionError("hostile numeric behavior executed")


class _TupleAlias(tuple):
    pass


class _MessageAlias(ModelMessage):
    pass


def test_model_candidate_rejects_behavioral_identity_before_string_methods() -> None:
    with pytest.raises(TypeError, match="candidate_id must be canonical text"):
        ModelCandidate(
            candidate_id=_HostileText("candidate"),
            provider_id="local",
            provider_kind=ProviderKind.LOCAL,
            request_model="model",
            expected_response_model="model",
            engine_provenance_ref="engine",
            engine_license_ref="engine-license",
            model_provenance_ref="model-ref",
            model_license_ref="model-license",
        )

    with pytest.raises(TypeError, match="ProviderKind"):
        ModelCandidate(
            candidate_id="candidate",
            provider_id="local",
            provider_kind="local",
            request_model="model",
            expected_response_model="model",
            engine_provenance_ref="engine",
            engine_license_ref="engine-license",
            model_provenance_ref="model-ref",
            model_license_ref="model-license",
        )


def test_evaluation_case_rejects_behavioral_numeric_before_float_conversion() -> None:
    with pytest.raises(TypeError, match="pass_score must be numeric"):
        EvaluationCase(
            case_id="case",
            messages=(ModelMessage("user", "prompt"),),
            expected_text="answer",
            pass_score=_HostileFloat(1.0),
        )


def test_evaluation_case_requires_exact_message_tuple_and_values() -> None:
    message = ModelMessage("user", "prompt")

    with pytest.raises(TypeError, match="canonical tuple"):
        EvaluationCase(
            case_id="case",
            messages=_TupleAlias((message,)),
            expected_text="answer",
        )

    with pytest.raises(TypeError, match="exact ModelMessage"):
        EvaluationCase(
            case_id="case",
            messages=(_MessageAlias("user", "prompt"),),
            expected_text="answer",
        )


def test_evaluation_set_requires_exact_enum_and_case_carriers() -> None:
    case = EvaluationCase(
        case_id="case",
        messages=(ModelMessage("user", "prompt"),),
        expected_text="answer",
    )

    with pytest.raises(TypeError, match="EvaluationPurpose"):
        EvaluationSet(
            evaluation_set_id="set",
            version="1",
            provenance_ref="dataset:set",
            license_ref="license:set",
            purpose="held_out",
            privacy=PrivacyClass.PUBLIC,
            cases=(case,),
        )

    with pytest.raises(TypeError, match="canonical tuple"):
        EvaluationSet(
            evaluation_set_id="set",
            version="1",
            provenance_ref="dataset:set",
            license_ref="license:set",
            purpose=EvaluationPurpose.HELD_OUT,
            privacy=PrivacyClass.PUBLIC,
            cases=_TupleAlias((case,)),
        )


def test_case_result_rejects_behavioral_metrics_before_conversion() -> None:
    with pytest.raises(TypeError, match="score must be numeric"):
        CaseBenchmarkResult(
            candidate_id="candidate",
            case_id="case",
            score=_HostileFloat(1.0),
            passed=True,
            completion_succeeded=True,
            latency_ms=1.0,
            response_sha256="a" * 64,
            error_code=None,
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            resource_before=None,
            resource_after=None,
            accelerator_before=None,
            accelerator_after=None,
        )


def test_case_result_rejects_impossible_completion_evidence() -> None:
    with pytest.raises(ValueError, match="passing quality evidence"):
        CaseBenchmarkResult(
            candidate_id="candidate",
            case_id="case",
            score=1.0,
            passed=True,
            completion_succeeded=False,
            latency_ms=1.0,
            response_sha256=None,
            error_code=ModelErrorCode.UNAVAILABLE,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            resource_before=None,
            resource_after=None,
            accelerator_before=None,
            accelerator_after=None,
        )

    with pytest.raises(ValueError, match="requires response_sha256"):
        CaseBenchmarkResult(
            candidate_id="candidate",
            case_id="case",
            score=1.0,
            passed=True,
            completion_succeeded=True,
            latency_ms=1.0,
            response_sha256=None,
            error_code=None,
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            resource_before=None,
            resource_after=None,
            accelerator_before=None,
            accelerator_after=None,
        )

    with pytest.raises(ValueError, match="token evidence"):
        CaseBenchmarkResult(
            candidate_id="candidate",
            case_id="case",
            score=0.0,
            passed=False,
            completion_succeeded=False,
            latency_ms=1.0,
            response_sha256=None,
            error_code=ModelErrorCode.UNAVAILABLE,
            input_tokens=1,
            output_tokens=None,
            total_tokens=None,
            resource_before=None,
            resource_after=None,
            accelerator_before=None,
            accelerator_after=None,
        )


class _ResourceAlias(ResourceSnapshot):
    pass


def test_case_result_requires_exact_resource_snapshot_carriers() -> None:
    with pytest.raises(TypeError, match="exact ResourceSnapshot"):
        CaseBenchmarkResult(
            candidate_id="candidate",
            case_id="case",
            score=1.0,
            passed=True,
            completion_succeeded=True,
            latency_ms=1.0,
            response_sha256="a" * 64,
            error_code=None,
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            resource_before=_ResourceAlias(1.0, 2.0, 3),
            resource_after=None,
            accelerator_before=None,
            accelerator_after=None,
        )


class _ResponseAlias(ModelResponse):
    pass


class _AliasResponseGateway:
    async def complete(self, request):
        return _ResponseAlias(
            request_id=request.request_id,
            text="answer",
            provider_id=request.provider_id,
            provider_kind=request.provider_kind,
            model=request.model,
        )


class _HostileScorer:
    def score(self, case, response):
        return _HostileFloat(1.0)


def test_runner_rejects_behavioral_timeout_before_numeric_conversion() -> None:
    runner = ModelBenchmarkRunner(_FakeGateway())

    with pytest.raises(TypeError, match="timeout_seconds must be numeric"):
        asyncio.run(
            runner.benchmark(
                _candidate(),
                _evaluation_set(),
                timeout_seconds=_HostileFloat(1.0),
            )
        )


def test_runner_rejects_behavioral_clock_before_arithmetic() -> None:
    runner = ModelBenchmarkRunner(
        _FakeGateway(),
        clock=_Clock((_HostileFloat(1.0),)),
    )

    with pytest.raises(ModelBenchmarkError, match="clock returned"):
        asyncio.run(runner.benchmark(_candidate(), _evaluation_set()))


def test_runner_rejects_response_subclass_before_identity_access() -> None:
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
        _AliasResponseGateway(),
        clock=_Clock((1.0,)),
    )

    with pytest.raises(ModelBenchmarkError, match="invalid response carrier"):
        asyncio.run(runner.benchmark(_candidate(), evaluation))


def test_runner_rejects_behavioral_scorer_result_before_conversion() -> None:
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
        _ProviderKindSpyGateway(),
        scorer=_HostileScorer(),
        scorer_id="hostile-test-v1",
        clock=_Clock((1.0, 1.1)),
    )

    with pytest.raises(ModelBenchmarkError, match="non-canonical numeric score"):
        asyncio.run(runner.benchmark(_candidate(), evaluation))


class _FalseyScorer:
    def __bool__(self):
        raise AssertionError("scorer truthiness executed")

    def score(self, case, response):
        return 1.0


class _HostileResponseFieldGateway:
    async def complete(self, request):
        return ModelResponse(
            request_id=_HostileText(request.request_id),
            text="answer",
            provider_id=request.provider_id,
            provider_kind=request.provider_kind,
            model=request.model,
        )


class _UsageAlias(ModelUsage):
    pass


class _UsageAliasGateway:
    async def complete(self, request):
        return ModelResponse(
            request_id=request.request_id,
            text="answer",
            provider_id=request.provider_id,
            provider_kind=request.provider_kind,
            model=request.model,
            usage=_UsageAlias(input_tokens=1, output_tokens=1, total_tokens=2),
        )


def test_runner_does_not_invoke_scorer_truthiness() -> None:
    runner = ModelBenchmarkRunner(
        _ProviderKindSpyGateway(),
        scorer=_FalseyScorer(),
        scorer_id="falsey-test-v1",
        clock=_Clock((1.0, 1.1, 2.0, 2.1)),
    )

    report = asyncio.run(runner.benchmark(_candidate(), _evaluation_set()))

    assert report.completion_rate == 1.0


def test_runner_rejects_behavioral_response_text_carrier_before_comparison() -> None:
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
        _HostileResponseFieldGateway(),
        clock=_Clock((1.0,)),
    )

    with pytest.raises(ModelBenchmarkError, match="request_id must be canonical text"):
        asyncio.run(runner.benchmark(_candidate(), evaluation))


def test_runner_requires_exact_usage_carrier() -> None:
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
        _UsageAliasGateway(),
        clock=_Clock((1.0,)),
    )

    with pytest.raises(ModelBenchmarkError, match="exact ModelUsage"):
        asyncio.run(runner.benchmark(_candidate(), evaluation))


def test_benchmark_suite_requires_canonical_candidate_tuple() -> None:
    candidate = _candidate()

    with pytest.raises(TypeError, match="canonical tuple"):
        asyncio.run(
            ModelBenchmarkRunner(_FakeGateway()).benchmark_suite(
                [candidate],
                _evaluation_set(),
            )
        )


def test_execution_config_identity_binds_timeout_and_temperature() -> None:
    baseline = BenchmarkExecutionConfig(timeout_seconds=60.0, temperature=0.0)
    changed_timeout = BenchmarkExecutionConfig(timeout_seconds=30.0, temperature=0.0)
    changed_temperature = BenchmarkExecutionConfig(timeout_seconds=60.0, temperature=0.5)
    changed_scorer = BenchmarkExecutionConfig(scorer_id="semantic-scorer-v2")

    assert baseline.evidence_sha256 != changed_timeout.evidence_sha256
    assert baseline.evidence_sha256 != changed_temperature.evidence_sha256
    assert baseline.evidence_sha256 != changed_scorer.evidence_sha256
    assert changed_timeout.evidence_sha256 != changed_temperature.evidence_sha256


class _ConfigMetadataGateway:
    def __init__(self) -> None:
        self.config_sha256 = None
        self.configuration_sha256 = None
        self.run_id = None
        self.request_id = None
        self.timeout_seconds = None
        self.temperature = None

    async def complete(self, request):
        self.config_sha256 = request.metadata["benchmark_execution_config_sha256"]
        self.configuration_sha256 = request.metadata["benchmark_configuration_sha256"]
        self.run_id = request.metadata["benchmark_run_id"]
        self.request_id = request.request_id
        self.timeout_seconds = request.timeout_seconds
        self.temperature = request.temperature
        return ModelResponse(
            request_id=request.request_id,
            text="answer",
            provider_id=request.provider_id,
            provider_kind=request.provider_kind,
            model=request.model,
        )


def test_benchmark_binds_execution_config_to_request_and_report() -> None:
    config = BenchmarkExecutionConfig(timeout_seconds=17.0, temperature=0.25)
    gateway = _ConfigMetadataGateway()
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
    report = asyncio.run(
        ModelBenchmarkRunner(gateway, clock=_Clock((1.0, 1.1))).benchmark(
            _candidate(),
            evaluation,
            timeout_seconds=config.timeout_seconds,
            temperature=config.temperature,
        )
    )

    assert gateway.config_sha256 == config.evidence_sha256
    assert gateway.timeout_seconds == config.timeout_seconds
    assert gateway.temperature == config.temperature
    assert report.execution_config_sha256 == config.evidence_sha256
    assert gateway.run_id == report.run.run_id
    assert gateway.configuration_sha256 == report.run.configuration_sha256
    assert report.run.configuration_sha256 == benchmark_configuration_sha256(
        candidate_evidence_sha256=report.candidate.evidence_sha256,
        evaluation_set_id=report.evaluation_set_id,
        evaluation_set_version=report.evaluation_set_version,
        evaluation_set_sha256=report.evaluation_set_sha256,
        execution_config_sha256=report.execution_config_sha256,
    )


def test_request_identity_changes_with_bound_execution_config() -> None:
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
    baseline_gateway = _ConfigMetadataGateway()
    changed_gateway = _ConfigMetadataGateway()
    baseline = BenchmarkExecutionConfig(timeout_seconds=60.0, temperature=0.0)
    changed = BenchmarkExecutionConfig(timeout_seconds=30.0, temperature=0.5)

    baseline_report = asyncio.run(
        ModelBenchmarkRunner(
            baseline_gateway,
            clock=_Clock((1.0, 1.1)),
        ).benchmark(
            _candidate(),
            evaluation,
            timeout_seconds=baseline.timeout_seconds,
            temperature=baseline.temperature,
        )
    )
    changed_report = asyncio.run(
        ModelBenchmarkRunner(
            changed_gateway,
            clock=_Clock((2.0, 2.1)),
        ).benchmark(
            _candidate(),
            evaluation,
            timeout_seconds=changed.timeout_seconds,
            temperature=changed.temperature,
        )
    )

    assert baseline_gateway.request_id != changed_gateway.request_id
    assert baseline_gateway.config_sha256 == baseline_report.execution_config_sha256
    assert changed_gateway.config_sha256 == changed_report.execution_config_sha256
    assert baseline_report.execution_config_sha256 != changed_report.execution_config_sha256
    assert baseline_report.run.configuration_sha256 != changed_report.run.configuration_sha256


def test_repeat_run_keeps_configuration_identity_but_changes_attempt_identity() -> None:
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
    run_ids = iter(("run-first", "run-second"))
    gateway = _ConfigMetadataGateway()
    runner = ModelBenchmarkRunner(
        gateway,
        clock=_Clock((1.0, 1.1, 2.0, 2.1)),
        run_id_factory=lambda: next(run_ids),
    )

    first = asyncio.run(runner.benchmark(_candidate(), evaluation))
    first_request_id = gateway.request_id
    second = asyncio.run(runner.benchmark(_candidate(), evaluation))

    assert first.run.run_id == "run-first"
    assert second.run.run_id == "run-second"
    assert first.run.configuration_sha256 == second.run.configuration_sha256
    assert first_request_id != gateway.request_id
    assert first.execution_config_sha256 == second.execution_config_sha256


@pytest.mark.parametrize("bad_run_id", ["", " leading", "trailing ", "bad/run", "x" * 129])
def test_run_id_factory_fails_closed_before_gateway_effect(bad_run_id: str) -> None:
    gateway = _ConfigMetadataGateway()
    runner = ModelBenchmarkRunner(
        gateway,
        run_id_factory=lambda: bad_run_id,
    )

    with pytest.raises((TypeError, ValueError), match="run_id"):
        asyncio.run(runner.benchmark(_candidate(), _evaluation_set()))

    assert gateway.request_id is None


def test_custom_scorer_requires_stable_identity_before_execution() -> None:
    with pytest.raises(TypeError, match="custom scorer requires a canonical scorer_id"):
        ModelBenchmarkRunner(_FakeGateway(), scorer=_FalseyScorer())


def test_execution_config_rejects_behavioral_scorer_id_before_string_methods() -> None:
    with pytest.raises(TypeError, match="scorer_id must be canonical text"):
        BenchmarkExecutionConfig(scorer_id=_HostileText("scorer-v1"))


class _HostileBenchmarkEnvelope:
    @property
    def cases(self):
        raise AssertionError("benchmark envelope fields must not be accessed")


def test_benchmark_requires_exact_candidate_and_evaluation_envelopes() -> None:
    runner = ModelBenchmarkRunner(_FakeGateway())

    with pytest.raises(TypeError, match="candidate must be an exact ModelCandidate"):
        asyncio.run(
            runner.benchmark(
                _HostileBenchmarkEnvelope(),  # type: ignore[arg-type]
                _evaluation_set(),
            )
        )
    with pytest.raises(TypeError, match="evaluation_set must be an exact EvaluationSet"):
        asyncio.run(
            runner.benchmark(
                _candidate(),
                _HostileBenchmarkEnvelope(),  # type: ignore[arg-type]
            )
        )


def test_benchmark_suite_requires_exact_evaluation_envelope() -> None:
    runner = ModelBenchmarkRunner(_FakeGateway())

    with pytest.raises(TypeError, match="evaluation_set must be an exact EvaluationSet"):
        asyncio.run(
            runner.benchmark_suite(
                (_candidate(),),
                _HostileBenchmarkEnvelope(),  # type: ignore[arg-type]
            )
        )


def test_model_lab_evidence_identities_reject_nonprintable_text() -> None:
    with pytest.raises(ValueError, match="control characters"):
        _candidate(candidate_id="bad\x00candidate")

    with pytest.raises(ValueError, match="control characters"):
        BenchmarkExecutionConfig(scorer_id="bad\x00scorer")


def test_evaluation_case_rejects_non_utf8_prompt_and_expected_text() -> None:
    invalid_utf8 = chr(0xD800)

    with pytest.raises(ValueError, match="message content must be valid UTF-8"):
        EvaluationCase(
            case_id="bad-prompt",
            messages=(ModelMessage("user", invalid_utf8),),
            expected_text="answer",
        )

    with pytest.raises(ValueError, match="expected_text must be valid UTF-8"):
        EvaluationCase(
            case_id="bad-expected",
            messages=(ModelMessage("user", "prompt"),),
            expected_text=invalid_utf8,
        )


class _NonUtf8ResponseGateway:
    async def complete(self, request):
        return ModelResponse(
            request_id=request.request_id,
            text=chr(0xD800),
            provider_id=request.provider_id,
            provider_kind=request.provider_kind,
            model=request.model,
        )


def test_runner_rejects_non_utf8_response_before_hashing() -> None:
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
        _NonUtf8ResponseGateway(),
        clock=_Clock((1.0,)),
    )

    with pytest.raises(ModelBenchmarkError, match="response text must be valid UTF-8"):
        asyncio.run(runner.benchmark(_candidate(), evaluation))


def test_benchmark_revalidates_mutated_message_before_provider_effect() -> None:
    gateway = _CountingGateway()
    evaluation = _evaluation_set()
    message = evaluation.cases[0].messages[0]
    object.__setattr__(message, "role", "forged-role")

    with pytest.raises(ValueError, match="unsupported message role"):
        asyncio.run(ModelBenchmarkRunner(gateway).benchmark(_candidate(), evaluation))

    assert gateway.calls == 0


def test_benchmark_suite_preflights_every_candidate_before_first_effect() -> None:
    gateway = _CountingGateway()
    first = _candidate(candidate_id="first")
    second = _candidate(candidate_id="second")
    object.__setattr__(second, "provider_id", " padded-provider ")

    with pytest.raises(ValueError, match="surrounding whitespace"):
        asyncio.run(
            ModelBenchmarkRunner(gateway).benchmark_suite(
                (first, second),
                _evaluation_set(),
            )
        )

    assert gateway.calls == 0
