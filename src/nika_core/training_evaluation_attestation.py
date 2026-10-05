from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelFailureEffect,
    ModelGatewayError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ProviderKind,
)
from nika_core.training_evaluation_binding import TrainingEvaluationBinding

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _canonical_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty canonical text")
    if any(not character.isprintable() for character in value):
        raise ValueError(f"{name} must not contain control characters")
    return value


def _sha256(value: object, *, name: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be an exact lowercase SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True)
class LoadedModelArtifactAttestation:
    """Provider evidence for the exact artifact used by one inference effect."""

    request_id: str
    binding_sha256: str
    provider_id: str
    model_id: str
    artifact_sha256: str
    descriptor_digest: str
    attestor_id: str
    attestor_sha256: str

    def __post_init__(self) -> None:
        _canonical_text(self.request_id, name="request_id")
        _canonical_text(self.provider_id, name="provider_id")
        _canonical_text(self.model_id, name="model_id")
        _canonical_text(self.attestor_id, name="attestor_id")
        _sha256(self.binding_sha256, name="binding_sha256")
        _sha256(self.artifact_sha256, name="artifact_sha256")
        _sha256(self.descriptor_digest, name="descriptor_digest")
        _sha256(self.attestor_sha256, name="attestor_sha256")

    def revalidated(self) -> LoadedModelArtifactAttestation:
        if type(self) is not LoadedModelArtifactAttestation:
            raise TypeError(
                "attestation must be an exact LoadedModelArtifactAttestation"
            )
        try:
            return LoadedModelArtifactAttestation(
                request_id=self.request_id,
                binding_sha256=self.binding_sha256,
                provider_id=self.provider_id,
                model_id=self.model_id,
                artifact_sha256=self.artifact_sha256,
                descriptor_digest=self.descriptor_digest,
                attestor_id=self.attestor_id,
                attestor_sha256=self.attestor_sha256,
            )
        except AttributeError as exc:
            raise ValueError("loaded-model attestation fields are incomplete") from exc


@dataclass(frozen=True, slots=True)
class AttestedModelCompletionResult:
    """One provider effect result: response plus same-effect loaded-artifact evidence."""

    response: ModelResponse
    attestation: LoadedModelArtifactAttestation


class LoadedModelAttestedCompletionPort(Protocol):
    """Provider-specific atomic/causally-bound inference + loaded-artifact proof."""

    async def complete_attested(
        self,
        request: ModelRequest,
        *,
        binding: TrainingEvaluationBinding,
    ) -> AttestedModelCompletionResult: ...


def _snapshot_request(request: ModelRequest) -> ModelRequest:
    if type(request) is not ModelRequest:
        raise TypeError("request must be an exact ModelRequest")
    try:
        return ModelRequest(
            request_id=request.request_id,
            messages=request.messages,
            model=request.model,
            provider_id=request.provider_id,
            provider_kind=request.provider_kind,
            fallback_provider_ids=request.fallback_provider_ids,
            privacy=request.privacy,
            timeout_seconds=request.timeout_seconds,
            temperature=request.temperature,
            metadata=dict(request.metadata),
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ModelGatewayError(
            ModelErrorCode.INVALID_REQUEST,
            "attested training-candidate request is invalid",
            failure_effect=ModelFailureEffect.NO_EFFECT,
        ) from exc


def _request_matches_binding(
    request: ModelRequest,
    binding: TrainingEvaluationBinding,
) -> None:
    if request.provider_kind is not ProviderKind.LOCAL:
        raise ModelGatewayError(
            ModelErrorCode.INVALID_REQUEST,
            "attested training candidate requires the local provider boundary",
            provider_id=binding.challenger_provider_id,
            failure_effect=ModelFailureEffect.NO_EFFECT,
        )
    if (
        request.provider_id != binding.challenger_provider_id
        or request.model != binding.challenger_model_id
    ):
        raise ModelGatewayError(
            ModelErrorCode.INVALID_REQUEST,
            "attested training-candidate route does not match evaluation binding",
            provider_id=binding.challenger_provider_id,
            failure_effect=ModelFailureEffect.NO_EFFECT,
        )
    if request.fallback_provider_ids:
        raise ModelGatewayError(
            ModelErrorCode.INVALID_REQUEST,
            "attested training-candidate inference cannot use provider fallback",
            provider_id=binding.challenger_provider_id,
            failure_effect=ModelFailureEffect.NO_EFFECT,
        )
    if request.metadata.get("model_candidate_id") != binding.challenger_candidate_id:
        raise ModelGatewayError(
            ModelErrorCode.INVALID_REQUEST,
            "benchmark candidate identity does not match evaluation binding",
            provider_id=binding.challenger_provider_id,
            failure_effect=ModelFailureEffect.NO_EFFECT,
        )


def _response_matches_request(
    response: ModelResponse,
    *,
    request: ModelRequest,
    binding: TrainingEvaluationBinding,
) -> None:
    if type(response) is not ModelResponse:
        raise ModelGatewayError(
            ModelErrorCode.PROVIDER_ERROR,
            "attested provider returned an invalid response carrier",
            provider_id=binding.challenger_provider_id,
            failure_effect=ModelFailureEffect.UNKNOWN,
        )
    if (
        type(response.request_id) is not str
        or response.request_id != request.request_id
        or type(response.provider_id) is not str
        or response.provider_id != binding.challenger_provider_id
        or response.provider_kind is not ProviderKind.LOCAL
        or type(response.model) is not str
        or response.model != binding.challenger_model_id
    ):
        raise ModelGatewayError(
            ModelErrorCode.PROVIDER_ERROR,
            "attested provider response identity is inconsistent",
            provider_id=binding.challenger_provider_id,
            failure_effect=ModelFailureEffect.UNKNOWN,
        )
    if type(response.text) is not str or not response.text:
        raise ModelGatewayError(
            ModelErrorCode.PROVIDER_ERROR,
            "attested provider response text is invalid",
            provider_id=binding.challenger_provider_id,
            failure_effect=ModelFailureEffect.UNKNOWN,
        )
    try:
        response.text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ModelGatewayError(
            ModelErrorCode.PROVIDER_ERROR,
            "attested provider response text is invalid",
            provider_id=binding.challenger_provider_id,
            failure_effect=ModelFailureEffect.UNKNOWN,
        ) from exc
    if type(response.usage) is not ModelUsage:
        raise ModelGatewayError(
            ModelErrorCode.PROVIDER_ERROR,
            "attested provider response usage is invalid",
            provider_id=binding.challenger_provider_id,
            failure_effect=ModelFailureEffect.UNKNOWN,
        )


def _attestation_matches_effect(
    attestation: LoadedModelArtifactAttestation,
    *,
    request: ModelRequest,
    binding: TrainingEvaluationBinding,
    expected_attestor_id: str,
    expected_attestor_sha256: str,
) -> None:
    try:
        observed = attestation.revalidated()
    except (AttributeError, TypeError, ValueError) as exc:
        raise ModelGatewayError(
            ModelErrorCode.PROVIDER_ERROR,
            "loaded-model attestation is invalid",
            provider_id=binding.challenger_provider_id,
            failure_effect=ModelFailureEffect.UNKNOWN,
        ) from exc

    if (
        observed.request_id != request.request_id
        or observed.binding_sha256 != binding.binding_sha256
        or observed.provider_id != binding.challenger_provider_id
        or observed.model_id != binding.challenger_model_id
        or observed.artifact_sha256 != binding.challenger_sha256
        or observed.descriptor_digest != binding.descriptor_digest
        or observed.attestor_id != expected_attestor_id
        or observed.attestor_sha256 != expected_attestor_sha256
    ):
        raise ModelGatewayError(
            ModelErrorCode.PROVIDER_ERROR,
            "loaded-model attestation does not match the inference effect",
            provider_id=binding.challenger_provider_id,
            failure_effect=ModelFailureEffect.UNKNOWN,
        )


class AttestedTrainingCandidateGateway:
    """Model-Lab completion adapter requiring same-effect loaded-artifact proof.

    The injected effect port must couple its response and loaded-artifact attestation
    to the same provider inference. This wrapper validates that proof before returning
    success to ModelBenchmarkRunner. Post-effect proof failures are UNKNOWN, never
    NO_EFFECT, so evaluation cannot silently retry or count an unattested response.
    """

    def __init__(
        self,
        effect_port: LoadedModelAttestedCompletionPort,
        *,
        binding: TrainingEvaluationBinding,
        expected_attestor_id: str,
        expected_attestor_sha256: str,
    ) -> None:
        try:
            canonical_binding = binding.revalidated()
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("binding must be canonical") from exc
        self._effect_port = effect_port
        self._binding = canonical_binding
        self._expected_attestor_id = _canonical_text(
            expected_attestor_id,
            name="expected_attestor_id",
        )
        self._expected_attestor_sha256 = _sha256(
            expected_attestor_sha256,
            name="expected_attestor_sha256",
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        canonical_request = _snapshot_request(request)
        _request_matches_binding(canonical_request, self._binding)

        result = await self._effect_port.complete_attested(
            canonical_request,
            binding=self._binding,
        )
        if type(result) is not AttestedModelCompletionResult:
            raise ModelGatewayError(
                ModelErrorCode.PROVIDER_ERROR,
                "attested provider returned an invalid effect result",
                provider_id=self._binding.challenger_provider_id,
                failure_effect=ModelFailureEffect.UNKNOWN,
            )
        _attestation_matches_effect(
            result.attestation,
            request=canonical_request,
            binding=self._binding,
            expected_attestor_id=self._expected_attestor_id,
            expected_attestor_sha256=self._expected_attestor_sha256,
        )
        _response_matches_request(
            result.response,
            request=canonical_request,
            binding=self._binding,
        )
        return result.response
