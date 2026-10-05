from __future__ import annotations

import hashlib

import pytest

from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelFailureEffect,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    PrivacyClass,
    ProviderKind,
)
from nika_core.training_evaluation_attestation import (
    AttestedModelCompletionResult,
    AttestedTrainingCandidateGateway,
    LoadedModelArtifactAttestation,
)
from nika_core.training_evaluation_binding import TrainingEvaluationBinding


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


_ATTESTOR_ID = "loop-c-test-attestor"
_ATTESTOR_SHA256 = _sha(b"attestor")
_CHALLENGER_SHA256 = _sha(b"challenger")
_DESCRIPTOR_SHA256 = _sha(b"descriptor")
_REGISTRY_KEY = _sha(b"registry")
_EVALUATION_SET_SHA256 = _sha(b"evaluation")


def _binding() -> TrainingEvaluationBinding:
    return TrainingEvaluationBinding(
        job_id="job-1",
        base_candidate_id="models/base",
        base_provider_id="ollama",
        base_model_id="base-model",
        challenger_candidate_id="models/candidate/job-1",
        challenger_provider_id="ollama",
        challenger_model_id="candidate-model",
        base_sha256=_sha(b"base"),
        challenger_sha256=_CHALLENGER_SHA256,
        candidate_artifact_ref="models/candidate/job-1",
        frozen_package_sha256=_sha(b"package"),
        execution_plan_sha256=_sha(b"training-execution-plan"),
        evaluation_set_sha256=_EVALUATION_SET_SHA256,
        base_descriptor_digest=_sha(b"base-descriptor"),
        base_descriptor_registry_key=_sha(b"base-registry"),
        base_size_bytes=len(b"base"),
        descriptor_digest=_DESCRIPTOR_SHA256,
        descriptor_registry_key=_REGISTRY_KEY,
        challenger_size_bytes=123,
    )


def _request(
    *,
    provider_id: str = "ollama",
    model: str = "candidate-model",
    candidate_id: str = "models/candidate/job-1",
    evaluation_set_sha256: str = _EVALUATION_SET_SHA256,
    fallbacks: tuple[str, ...] = (),
) -> ModelRequest:
    return ModelRequest(
        request_id="benchmark-request-1",
        messages=(ModelMessage(role="user", content="question"),),
        model=model,
        provider_id=provider_id,
        provider_kind=ProviderKind.LOCAL,
        fallback_provider_ids=fallbacks,
        privacy=PrivacyClass.PRIVATE,
        metadata={
            "model_candidate_id": candidate_id,
            "evaluation_set_sha256": evaluation_set_sha256,
        },
    )


class _EffectPort:
    def __init__(self, *, mode: str = "valid") -> None:
        self.mode = mode
        self.calls = 0

    async def complete_attested(
        self,
        request: ModelRequest,
        *,
        binding: TrainingEvaluationBinding,
    ) -> AttestedModelCompletionResult:
        self.calls += 1
        if self.mode == "mutate-effect-binding":
            object.__setattr__(binding, "challenger_sha256", _sha(b"substituted-artifact"))
        elif self.mode == "mutate-effect-request":
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
        response = ModelResponse(
            request_id=request.request_id,
            text="answer",
            provider_id=binding.challenger_provider_id,
            provider_kind=ProviderKind.LOCAL,
            model=binding.challenger_model_id,
            usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
        )

        if self.mode == "wrong-artifact":
            attestation = LoadedModelArtifactAttestation(
                request_id=attestation.request_id,
                binding_sha256=attestation.binding_sha256,
                provider_id=attestation.provider_id,
                model_id=attestation.model_id,
                artifact_sha256=_sha(b"wrong-artifact"),
                descriptor_digest=attestation.descriptor_digest,
                attestor_id=attestation.attestor_id,
                attestor_sha256=attestation.attestor_sha256,
            )
        elif self.mode == "wrong-request":
            attestation = LoadedModelArtifactAttestation(
                request_id="different-request",
                binding_sha256=attestation.binding_sha256,
                provider_id=attestation.provider_id,
                model_id=attestation.model_id,
                artifact_sha256=attestation.artifact_sha256,
                descriptor_digest=attestation.descriptor_digest,
                attestor_id=attestation.attestor_id,
                attestor_sha256=attestation.attestor_sha256,
            )
        elif self.mode == "wrong-attestor":
            attestation = LoadedModelArtifactAttestation(
                request_id=attestation.request_id,
                binding_sha256=attestation.binding_sha256,
                provider_id=attestation.provider_id,
                model_id=attestation.model_id,
                artifact_sha256=attestation.artifact_sha256,
                descriptor_digest=attestation.descriptor_digest,
                attestor_id="different-attestor",
                attestor_sha256=attestation.attestor_sha256,
            )
        elif self.mode == "wrong-response-model":
            response = ModelResponse(
                request_id=request.request_id,
                text="answer",
                provider_id=binding.challenger_provider_id,
                provider_kind=ProviderKind.LOCAL,
                model="other-model",
            )
        elif self.mode == "mutated-attestation":
            object.__setattr__(attestation, "artifact_sha256", "not-a-digest")

        return AttestedModelCompletionResult(
            response=response,
            attestation=attestation,
        )


def _gateway(port: _EffectPort, binding: TrainingEvaluationBinding | None = None):
    return AttestedTrainingCandidateGateway(
        port,
        binding=binding or _binding(),
        expected_attestor_id=_ATTESTOR_ID,
        expected_attestor_sha256=_ATTESTOR_SHA256,
    )


@pytest.mark.asyncio
async def test_valid_same_effect_attestation_allows_challenger_response() -> None:
    port = _EffectPort()
    gateway = _gateway(port)

    response = await gateway.complete(_request())

    assert port.calls == 1
    assert response.provider_id == "ollama"
    assert response.model == "candidate-model"
    assert response.text == "answer"


@pytest.mark.asyncio
async def test_route_mismatch_is_rejected_before_provider_effect() -> None:
    port = _EffectPort()
    gateway = _gateway(port)

    with pytest.raises(ModelGatewayError) as exc_info:
        await gateway.complete(_request(provider_id="other-local"))

    assert exc_info.value.code is ModelErrorCode.INVALID_REQUEST
    assert exc_info.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert port.calls == 0


@pytest.mark.asyncio
async def test_fallback_route_is_rejected_before_provider_effect() -> None:
    port = _EffectPort()
    gateway = _gateway(port)

    with pytest.raises(ModelGatewayError) as exc_info:
        await gateway.complete(_request(fallbacks=("other-local",)))

    assert exc_info.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert port.calls == 0


@pytest.mark.asyncio
async def test_candidate_identity_mismatch_is_rejected_before_effect() -> None:
    port = _EffectPort()
    gateway = _gateway(port)

    with pytest.raises(ModelGatewayError) as exc_info:
        await gateway.complete(_request(candidate_id="different-candidate"))

    assert exc_info.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert port.calls == 0


@pytest.mark.asyncio
async def test_evaluation_set_identity_mismatch_is_rejected_before_effect() -> None:
    port = _EffectPort()
    gateway = _gateway(port)

    with pytest.raises(ModelGatewayError) as exc_info:
        await gateway.complete(
            _request(evaluation_set_sha256=_sha(b"different-held-out"))
        )

    assert exc_info.value.code is ModelErrorCode.INVALID_REQUEST
    assert exc_info.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert port.calls == 0


@pytest.mark.asyncio
async def test_wrong_loaded_artifact_is_unknown_after_provider_effect() -> None:
    port = _EffectPort(mode="wrong-artifact")
    gateway = _gateway(port)

    with pytest.raises(ModelGatewayError) as exc_info:
        await gateway.complete(_request())

    assert exc_info.value.code is ModelErrorCode.PROVIDER_ERROR
    assert exc_info.value.failure_effect is ModelFailureEffect.UNKNOWN
    assert port.calls == 1


@pytest.mark.asyncio
async def test_attestation_request_mismatch_is_unknown_after_effect() -> None:
    port = _EffectPort(mode="wrong-request")
    gateway = _gateway(port)

    with pytest.raises(ModelGatewayError) as exc_info:
        await gateway.complete(_request())

    assert exc_info.value.failure_effect is ModelFailureEffect.UNKNOWN
    assert port.calls == 1


@pytest.mark.asyncio
async def test_attestor_identity_mismatch_is_unknown_after_effect() -> None:
    port = _EffectPort(mode="wrong-attestor")
    gateway = _gateway(port)

    with pytest.raises(ModelGatewayError) as exc_info:
        await gateway.complete(_request())

    assert exc_info.value.failure_effect is ModelFailureEffect.UNKNOWN
    assert port.calls == 1


@pytest.mark.asyncio
async def test_response_model_mismatch_is_unknown_after_effect() -> None:
    port = _EffectPort(mode="wrong-response-model")
    gateway = _gateway(port)

    with pytest.raises(ModelGatewayError) as exc_info:
        await gateway.complete(_request())

    assert exc_info.value.failure_effect is ModelFailureEffect.UNKNOWN
    assert port.calls == 1


@pytest.mark.asyncio
async def test_mutated_attestation_carrier_is_revalidated() -> None:
    port = _EffectPort(mode="mutated-attestation")
    gateway = _gateway(port)

    with pytest.raises(ModelGatewayError) as exc_info:
        await gateway.complete(_request())

    assert exc_info.value.failure_effect is ModelFailureEffect.UNKNOWN
    assert port.calls == 1


@pytest.mark.asyncio
async def test_invalid_effect_result_carrier_is_unknown() -> None:
    class BadPort:
        calls = 0

        async def complete_attested(self, request, *, binding):
            self.calls += 1
            return object()

    port = BadPort()
    gateway = AttestedTrainingCandidateGateway(
        port,  # type: ignore[arg-type]
        binding=_binding(),
        expected_attestor_id=_ATTESTOR_ID,
        expected_attestor_sha256=_ATTESTOR_SHA256,
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        await gateway.complete(_request())

    assert exc_info.value.failure_effect is ModelFailureEffect.UNKNOWN
    assert port.calls == 1


def test_mutated_binding_is_rejected_before_gateway_use() -> None:
    binding = _binding()
    object.__setattr__(binding, "challenger_model_id", "")

    with pytest.raises(ValueError, match="binding must be canonical"):
        _gateway(_EffectPort(), binding=binding)


@pytest.mark.asyncio
async def test_effect_port_gateway_failure_preserves_original_effect_truth() -> None:
    class FailingPort:
        async def complete_attested(self, request, *, binding):
            raise ModelGatewayError(
                ModelErrorCode.TIMEOUT,
                "provider timed out",
                provider_id=binding.challenger_provider_id,
                failure_effect=ModelFailureEffect.UNKNOWN,
            )

    gateway = AttestedTrainingCandidateGateway(
        FailingPort(),
        binding=_binding(),
        expected_attestor_id=_ATTESTOR_ID,
        expected_attestor_sha256=_ATTESTOR_SHA256,
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        await gateway.complete(_request())

    assert exc_info.value.code is ModelErrorCode.TIMEOUT
    assert exc_info.value.failure_effect is ModelFailureEffect.UNKNOWN


def test_gateway_rejects_unbounded_attestor_identity() -> None:
    with pytest.raises(ValueError, match="configured byte limit"):
        AttestedTrainingCandidateGateway(
            _EffectPort(),
            binding=_binding(),
            expected_attestor_id="a" * 513,
            expected_attestor_sha256=_ATTESTOR_SHA256,
        )


@pytest.mark.asyncio
async def test_invalid_usage_bool_is_unknown_after_provider_effect() -> None:
    class BadUsagePort(_EffectPort):
        async def complete_attested(
            self,
            request: ModelRequest,
            *,
            binding: TrainingEvaluationBinding,
        ) -> AttestedModelCompletionResult:
            result = await super().complete_attested(request, binding=binding)
            bad_response = ModelResponse(
                request_id=result.response.request_id,
                text=result.response.text,
                provider_id=result.response.provider_id,
                provider_kind=result.response.provider_kind,
                model=result.response.model,
                usage=ModelUsage(input_tokens=True, output_tokens=1, total_tokens=2),
            )
            return AttestedModelCompletionResult(
                response=bad_response,
                attestation=result.attestation,
            )

    port = BadUsagePort()
    gateway = _gateway(port)

    with pytest.raises(ModelGatewayError) as exc_info:
        await gateway.complete(_request())

    assert exc_info.value.failure_effect is ModelFailureEffect.UNKNOWN
    assert port.calls == 1


@pytest.mark.asyncio
async def test_inconsistent_usage_total_is_unknown_after_provider_effect() -> None:
    class BadUsagePort(_EffectPort):
        async def complete_attested(
            self,
            request: ModelRequest,
            *,
            binding: TrainingEvaluationBinding,
        ) -> AttestedModelCompletionResult:
            result = await super().complete_attested(request, binding=binding)
            bad_response = ModelResponse(
                request_id=result.response.request_id,
                text=result.response.text,
                provider_id=result.response.provider_id,
                provider_kind=result.response.provider_kind,
                model=result.response.model,
                usage=ModelUsage(input_tokens=2, output_tokens=2, total_tokens=3),
            )
            return AttestedModelCompletionResult(
                response=bad_response,
                attestation=result.attestation,
            )

    port = BadUsagePort()
    gateway = _gateway(port)

    with pytest.raises(ModelGatewayError) as exc_info:
        await gateway.complete(_request())

    assert exc_info.value.failure_effect is ModelFailureEffect.UNKNOWN
    assert port.calls == 1


@pytest.mark.asyncio
async def test_mutated_unbounded_attestation_identity_is_unknown() -> None:
    class BadAttestationPort(_EffectPort):
        async def complete_attested(
            self,
            request: ModelRequest,
            *,
            binding: TrainingEvaluationBinding,
        ) -> AttestedModelCompletionResult:
            result = await super().complete_attested(request, binding=binding)
            object.__setattr__(result.attestation, "attestor_id", "a" * 513)
            return result

    port = BadAttestationPort()
    gateway = _gateway(port)

    with pytest.raises(ModelGatewayError) as exc_info:
        await gateway.complete(_request())

    assert exc_info.value.failure_effect is ModelFailureEffect.UNKNOWN
    assert port.calls == 1



@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["mutate-effect-binding", "mutate-effect-request"])
async def test_effect_port_cannot_mutate_verification_authority(mode: str) -> None:
    port = _EffectPort(mode=mode)
    gateway = _gateway(port)

    with pytest.raises(ModelGatewayError) as exc_info:
        await gateway.complete(_request())

    assert exc_info.value.code is ModelErrorCode.PROVIDER_ERROR
    assert exc_info.value.failure_effect is ModelFailureEffect.UNKNOWN
    assert port.calls == 1


@pytest.mark.asyncio
async def test_untyped_effect_port_failure_is_unknown_and_secret_free() -> None:
    class ExplodingPort:
        async def complete_attested(self, request, *, binding):
            raise RuntimeError("raw provider detail must not escape")

    gateway = AttestedTrainingCandidateGateway(
        ExplodingPort(),
        binding=_binding(),
        expected_attestor_id=_ATTESTOR_ID,
        expected_attestor_sha256=_ATTESTOR_SHA256,
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        await gateway.complete(_request())

    assert exc_info.value.code is ModelErrorCode.PROVIDER_ERROR
    assert exc_info.value.failure_effect is ModelFailureEffect.UNKNOWN
    assert "raw provider detail" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


@pytest.mark.asyncio
async def test_non_request_carrier_is_typed_no_effect_failure() -> None:
    port = _EffectPort()
    gateway = _gateway(port)

    with pytest.raises(ModelGatewayError) as exc_info:
        await gateway.complete(object())  # type: ignore[arg-type]

    assert exc_info.value.code is ModelErrorCode.INVALID_REQUEST
    assert exc_info.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert port.calls == 0
