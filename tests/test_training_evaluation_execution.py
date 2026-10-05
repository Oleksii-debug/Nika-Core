from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from nika_core.model_engineering import (
    EvaluationCase,
    EvaluationPurpose,
    EvaluationSet,
    ModelCandidate,
)
from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelFailureEffect,
    ModelGatewayError,
    ModelMessage,
    ModelResponse,
    ModelUsage,
    PrivacyClass,
    ProviderKind,
)
from nika_core.training_evaluation_attestation import (
    AttestedModelCompletionResult,
    LoadedModelArtifactAttestation,
)
from nika_core.training_evaluation_binding import TrainingEvaluationBinding
from nika_core.training_evaluation_execution import (
    AttestedChallengerBenchmarkResult,
    TrainingEvaluationExecutionError,
    run_attested_challenger_benchmark,
)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


_CHALLENGER_SHA256 = _sha(b"trained-challenger")
_ATTESTOR_SHA256 = _sha(b"attestor-code")
_ATTESTOR_ID = "test-loaded-model-attestor"
_DESCRIPTOR_SHA256 = _sha(b"descriptor")
_REGISTRY_KEY = _sha(b"registry")


def _evaluation_set(
    *,
    purpose: EvaluationPurpose = EvaluationPurpose.HELD_OUT,
) -> EvaluationSet:
    return EvaluationSet(
        evaluation_set_id="loop-c-held-out",
        version="2026-10-05.v1",
        provenance_ref="dataset:loop-c-held-out",
        license_ref="license:internal-eval",
        purpose=purpose,
        privacy=PrivacyClass.PRIVATE,
        cases=(
            EvaluationCase(
                case_id="case-1",
                messages=(ModelMessage(role="user", content="question one"),),
                expected_text="answer",
            ),
            EvaluationCase(
                case_id="case-2",
                messages=(ModelMessage(role="user", content="question two"),),
                expected_text="answer",
            ),
        ),
    )


def _binding(evaluation_set: EvaluationSet) -> TrainingEvaluationBinding:
    return TrainingEvaluationBinding(
        job_id="training-job-1",
        base_candidate_id="models/base",
        challenger_candidate_id="models/challenger",
        challenger_provider_id="ollama",
        challenger_model_id="challenger-model",
        base_sha256=_sha(b"base"),
        challenger_sha256=_CHALLENGER_SHA256,
        candidate_artifact_ref="models/challenger",
        frozen_package_sha256=_sha(b"package"),
        evaluation_set_sha256=evaluation_set.content_sha256,
        descriptor_digest=_DESCRIPTOR_SHA256,
        descriptor_registry_key=_REGISTRY_KEY,
        challenger_size_bytes=len(b"trained-challenger"),
    )


def _challenger() -> ModelCandidate:
    return ModelCandidate(
        candidate_id="models/challenger",
        provider_id="ollama",
        provider_kind=ProviderKind.LOCAL,
        request_model="challenger-model",
        expected_response_model="challenger-model",
        engine_provenance_ref="engine:ollama",
        engine_license_ref="license:ollama",
        model_provenance_ref="model:challenger",
        model_license_ref="license:challenger",
        model_sha256=_CHALLENGER_SHA256,
    )


class _AttestedEffectPort:
    def __init__(self, *, mode: str = "valid") -> None:
        self.mode = mode
        self.calls = 0

    async def complete_attested(
        self,
        request,
        *,
        binding: TrainingEvaluationBinding,
    ) -> AttestedModelCompletionResult:
        self.calls += 1
        if self.mode == "provider-no-effect":
            raise ModelGatewayError(
                ModelErrorCode.UNAVAILABLE,
                "private provider detail must not escape",
                provider_id=binding.challenger_provider_id,
                failure_effect=ModelFailureEffect.NO_EFFECT,
            )
        if self.mode == "untyped-provider-failure":
            raise RuntimeError("raw provider secret must not escape")
        if self.mode == "mutate-effect-request":
            object.__setattr__(request, "request_id", "substituted-request")
        attestation = LoadedModelArtifactAttestation(
            request_id=request.request_id,
            binding_sha256=binding.binding_sha256,
            provider_id=binding.challenger_provider_id,
            model_id=binding.challenger_model_id,
            artifact_sha256=binding.challenger_sha256,
            descriptor_digest=binding.descriptor_digest,
            attestor_id=_ATTESTOR_ID,
            attestor_sha256=_ATTESTOR_SHA256,
        )
        if self.mode == "wrong-artifact":
            attestation = replace(
                attestation,
                artifact_sha256=_sha(b"wrong-artifact"),
            )
        response = ModelResponse(
            request_id=request.request_id,
            text="answer",
            provider_id=binding.challenger_provider_id,
            provider_kind=ProviderKind.LOCAL,
            model=binding.challenger_model_id,
            usage=ModelUsage(input_tokens=2, output_tokens=1, total_tokens=3),
        )
        return AttestedModelCompletionResult(
            response=response,
            attestation=attestation,
        )


async def _run(
    port: _AttestedEffectPort,
    *,
    evaluation_set: EvaluationSet | None = None,
    binding: TrainingEvaluationBinding | None = None,
    challenger: ModelCandidate | None = None,
):
    evaluation = evaluation_set or _evaluation_set()
    return await run_attested_challenger_benchmark(
        binding=binding or _binding(evaluation),
        challenger=challenger or _challenger(),
        evaluation_set=evaluation,
        effect_port=port,
        expected_attestor_id=_ATTESTOR_ID,
        expected_attestor_sha256=_ATTESTOR_SHA256,
        timeout_seconds=5,
        temperature=0,
    )


@pytest.mark.asyncio
async def test_complete_attested_challenger_benchmark_emits_bound_evidence() -> None:
    port = _AttestedEffectPort()

    result = await _run(port)

    assert port.calls == 2
    assert result.report.completion_rate == 1.0
    assert all(item.completion_succeeded for item in result.report.case_results)
    assert [receipt.case_id for receipt in result.case_receipts] == [
        "case-1",
        "case-2",
    ]
    assert all(
        receipt.binding_sha256 == result.binding.binding_sha256
        for receipt in result.case_receipts
    )
    assert result.binding.binding_sha256 == result.evidence_payload()["binding_sha256"]
    assert result.evidence_payload()["case_count"] == 2
    assert len(result.evidence_payload()["case_receipts"]) == 2
    assert len(result.evidence_sha256) == 64
    assert result.revalidated().evidence_sha256 == result.evidence_sha256


@pytest.mark.asyncio
async def test_evidence_payload_omits_prompt_response_and_provider_exception_text() -> None:
    result = await _run(_AttestedEffectPort())

    body = json.dumps(result.evidence_payload(), ensure_ascii=False)

    assert "question one" not in body
    assert "question two" not in body
    assert '"answer"' not in body
    assert "private provider detail" not in body


@pytest.mark.asyncio
async def test_wrong_loaded_artifact_aborts_before_second_benchmark_case() -> None:
    port = _AttestedEffectPort(mode="wrong-artifact")

    with pytest.raises(TrainingEvaluationExecutionError) as exc_info:
        await _run(port)

    assert port.calls == 1
    assert exc_info.value.code is ModelErrorCode.PROVIDER_ERROR
    assert exc_info.value.failure_effect is ModelFailureEffect.UNKNOWN


@pytest.mark.asyncio
async def test_provider_failure_aborts_without_flattening_or_secret_text() -> None:
    port = _AttestedEffectPort(mode="provider-no-effect")

    with pytest.raises(TrainingEvaluationExecutionError) as exc_info:
        await _run(port)

    assert port.calls == 1
    assert exc_info.value.code is ModelErrorCode.UNAVAILABLE
    assert exc_info.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert "private provider detail" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_untyped_provider_failure_aborts_once_and_is_secret_free() -> None:
    port = _AttestedEffectPort(mode="untyped-provider-failure")

    with pytest.raises(TrainingEvaluationExecutionError) as exc_info:
        await _run(port)

    assert port.calls == 1
    assert exc_info.value.code is ModelErrorCode.PROVIDER_ERROR
    assert exc_info.value.failure_effect is ModelFailureEffect.UNKNOWN
    assert "raw provider secret" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_provider_input_mutation_aborts_before_second_case() -> None:
    port = _AttestedEffectPort(mode="mutate-effect-request")

    with pytest.raises(TrainingEvaluationExecutionError) as exc_info:
        await _run(port)

    assert port.calls == 1
    assert exc_info.value.code is ModelErrorCode.PROVIDER_ERROR
    assert exc_info.value.failure_effect is ModelFailureEffect.UNKNOWN


@pytest.mark.asyncio
async def test_challenger_digest_substitution_fails_before_provider_effect() -> None:
    port = _AttestedEffectPort()
    challenger = replace(_challenger(), model_sha256=_sha(b"other-model"))

    with pytest.raises(ValueError, match="challenger does not match"):
        await _run(port, challenger=challenger)

    assert port.calls == 0


@pytest.mark.asyncio
async def test_challenger_provider_substitution_fails_before_provider_effect() -> None:
    port = _AttestedEffectPort()
    challenger = replace(_challenger(), provider_id="foundry-local")

    with pytest.raises(ValueError, match="challenger does not match"):
        await _run(port, challenger=challenger)

    assert port.calls == 0


@pytest.mark.asyncio
async def test_development_evaluation_fails_before_provider_effect() -> None:
    port = _AttestedEffectPort()
    evaluation = _evaluation_set(purpose=EvaluationPurpose.DEVELOPMENT)

    with pytest.raises(ValueError, match="held-out"):
        await _run(port, evaluation_set=evaluation)

    assert port.calls == 0


@pytest.mark.asyncio
async def test_evaluation_identity_substitution_fails_before_provider_effect() -> None:
    port = _AttestedEffectPort()
    canonical = _evaluation_set()
    binding = _binding(canonical)
    changed = EvaluationSet(
        evaluation_set_id=canonical.evaluation_set_id,
        version="2026-10-05.v2",
        provenance_ref=canonical.provenance_ref,
        license_ref=canonical.license_ref,
        purpose=canonical.purpose,
        privacy=canonical.privacy,
        cases=canonical.cases,
    )

    with pytest.raises(ValueError, match="evaluation set does not match"):
        await _run(port, evaluation_set=changed, binding=binding)

    assert port.calls == 0


@pytest.mark.asyncio
async def test_mutated_binding_fails_revalidation_before_provider_effect() -> None:
    port = _AttestedEffectPort()
    evaluation = _evaluation_set()
    binding = _binding(evaluation)
    object.__setattr__(binding, "challenger_model_id", " padded-model ")

    with pytest.raises(ValueError, match="binding must be canonical"):
        await _run(port, evaluation_set=evaluation, binding=binding)

    assert port.calls == 0


@pytest.mark.asyncio
async def test_unbounded_attestor_identity_fails_before_provider_effect() -> None:
    port = _AttestedEffectPort()
    evaluation = _evaluation_set()

    with pytest.raises(ValueError, match="configured byte limit"):
        await run_attested_challenger_benchmark(
            binding=_binding(evaluation),
            challenger=_challenger(),
            evaluation_set=evaluation,
            effect_port=port,
            expected_attestor_id="a" * 513,
            expected_attestor_sha256=_ATTESTOR_SHA256,
        )

    assert port.calls == 0


@pytest.mark.asyncio
async def test_result_revalidation_rejects_mutated_report_aggregate() -> None:
    result = await _run(_AttestedEffectPort())
    object.__setattr__(result.report, "completion_rate", 0.5)

    with pytest.raises(ValueError, match="benchmark report must be canonical"):
        result.evidence_payload()


@pytest.mark.asyncio
async def test_result_revalidation_rejects_mutated_binding() -> None:
    result = await _run(_AttestedEffectPort())
    object.__setattr__(result.binding, "challenger_sha256", _sha(b"mutated"))

    with pytest.raises(
        ValueError,
        match="benchmark candidate does not match training binding",
    ):
        result.evidence_payload()


@pytest.mark.asyncio
async def test_result_revalidation_rejects_receipt_artifact_substitution() -> None:
    result = await _run(_AttestedEffectPort())
    object.__setattr__(
        result.case_receipts[0],
        "artifact_sha256",
        _sha(b"substituted-artifact"),
    )

    with pytest.raises(ValueError, match="receipt does not match benchmark authority"):
        result.evidence_payload()


@pytest.mark.asyncio
async def test_result_revalidation_rejects_receipt_request_substitution() -> None:
    result = await _run(_AttestedEffectPort())
    object.__setattr__(
        result.case_receipts[0],
        "request_id",
        "model-bench-substituted-request",
    )

    with pytest.raises(ValueError, match="request identity is inconsistent"):
        result.evidence_payload()


@pytest.mark.asyncio
async def test_result_revalidation_rejects_missing_case_receipt() -> None:
    result = await _run(_AttestedEffectPort())
    object.__setattr__(result, "case_receipts", result.case_receipts[:1])

    with pytest.raises(ValueError, match="receipt coverage"):
        result.evidence_payload()


def test_attested_benchmark_result_cannot_be_constructed_directly() -> None:
    with pytest.raises(TypeError):
        AttestedChallengerBenchmarkResult(
            binding=object(),
            report=object(),
            attestor_id=_ATTESTOR_ID,
            attestor_sha256=_ATTESTOR_SHA256,
        )


@pytest.mark.asyncio
async def test_caller_mutation_during_effect_cannot_retarget_snapshot() -> None:
    challenger = _challenger()
    evaluation = _evaluation_set()
    binding = _binding(evaluation)

    class MutatingPort(_AttestedEffectPort):
        async def complete_attested(
            self,
            request,
            *,
            binding: TrainingEvaluationBinding,
        ) -> AttestedModelCompletionResult:
            if self.calls == 0:
                object.__setattr__(challenger, "provider_id", "retargeted-provider")
                object.__setattr__(
                    evaluation.cases[1],
                    "expected_text",
                    "retargeted-answer",
                )
            return await super().complete_attested(request, binding=binding)

    port = MutatingPort()
    result = await run_attested_challenger_benchmark(
        binding=binding,
        challenger=challenger,
        evaluation_set=evaluation,
        effect_port=port,
        expected_attestor_id=_ATTESTOR_ID,
        expected_attestor_sha256=_ATTESTOR_SHA256,
        timeout_seconds=5,
        temperature=0,
    )

    assert port.calls == 2
    assert result.report.candidate.provider_id == "ollama"
    assert result.report.task_pass_rate == 1.0



@pytest.mark.asyncio
async def test_scorer_failure_after_provider_effect_is_unknown_and_secret_free() -> None:
    class ExplodingScorer:
        def score(self, case, response):
            del case, response
            raise RuntimeError("private scorer detail must not escape")

    port = _AttestedEffectPort()

    with pytest.raises(TrainingEvaluationExecutionError) as exc_info:
        evaluation = _evaluation_set()
        await run_attested_challenger_benchmark(
            binding=_binding(evaluation),
            challenger=_challenger(),
            evaluation_set=evaluation,
            effect_port=port,
            expected_attestor_id=_ATTESTOR_ID,
            expected_attestor_sha256=_ATTESTOR_SHA256,
            scorer=ExplodingScorer(),
            scorer_id="test-exploding-scorer-v1",
        )

    assert port.calls == 1
    assert exc_info.value.failure_effect is ModelFailureEffect.UNKNOWN
    assert "private scorer detail" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


@pytest.mark.asyncio
async def test_resource_observer_failure_before_provider_effect_is_no_effect() -> None:
    class ExplodingObserver:
        def snapshot(self):
            raise RuntimeError("private resource detail must not escape")

    port = _AttestedEffectPort()

    with pytest.raises(TrainingEvaluationExecutionError) as exc_info:
        evaluation = _evaluation_set()
        await run_attested_challenger_benchmark(
            binding=_binding(evaluation),
            challenger=_challenger(),
            evaluation_set=evaluation,
            effect_port=port,
            expected_attestor_id=_ATTESTOR_ID,
            expected_attestor_sha256=_ATTESTOR_SHA256,
            resource_observer=ExplodingObserver(),
        )

    assert port.calls == 0
    assert exc_info.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert "private resource detail" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
