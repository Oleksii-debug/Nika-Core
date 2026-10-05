from __future__ import annotations

import os
from pathlib import Path

from nika_core.model_artifacts import (
    ModelArtifactDescriptor,
    ModelArtifactKind,
    ModelArtifactResources,
    ModelIntegrityBasis,
)
from nika_core.model_engineering.contracts import (
    ModelCandidate,
    benchmark_configuration_sha256,
    validate_model_candidate,
)
from nika_core.model_engineering.runner import ModelCompletionPort
from nika_core.model_gateway.contracts import ModelRequest, ModelResponse, ProviderKind
from nika_core.training_artifacts import verify_candidate_artifact


class PhysicalCandidateEvaluationError(RuntimeError):
    """Safe failure from candidate-artifact evaluation admission."""


def _validate_descriptor(descriptor: ModelArtifactDescriptor) -> None:
    if type(descriptor) is not ModelArtifactDescriptor:
        raise TypeError("descriptor must be an exact ModelArtifactDescriptor")
    ModelArtifactDescriptor.__post_init__(descriptor)
    if type(descriptor.resources) is not ModelArtifactResources:
        raise TypeError("descriptor resources must be exact ModelArtifactResources")
    ModelArtifactResources.__post_init__(descriptor.resources)
    if descriptor.kind not in {
        ModelArtifactKind.EMBEDDED,
        ModelArtifactKind.EXTERNAL_LOCAL,
    }:
        raise PhysicalCandidateEvaluationError(
            "physical candidate evaluation requires a local model artifact"
        )
    if descriptor.integrity_basis is not ModelIntegrityBasis.SHA256:
        raise PhysicalCandidateEvaluationError(
            "physical candidate evaluation requires SHA-256 artifact integrity"
        )
    if descriptor.sha256 is None or descriptor.size_bytes is None:
        raise PhysicalCandidateEvaluationError(
            "physical candidate evaluation requires exact artifact digest and size"
        )


def _validate_candidate_binding(
    candidate: ModelCandidate,
    descriptor: ModelArtifactDescriptor,
) -> None:
    validate_model_candidate(candidate)
    _validate_descriptor(descriptor)
    if candidate.provider_kind is not ProviderKind.LOCAL:
        raise PhysicalCandidateEvaluationError(
            "physical candidate evaluation requires a local provider"
        )
    if candidate.model_sha256 is None:
        raise PhysicalCandidateEvaluationError(
            "physical candidate evaluation requires candidate model_sha256"
        )
    expected = (
        (candidate.provider_id, descriptor.provider_id, "provider identity"),
        (candidate.request_model, descriptor.model_id, "request model identity"),
        (
            candidate.expected_response_model,
            descriptor.model_id,
            "response model identity",
        ),
        (
            candidate.model_provenance_ref,
            descriptor.source_reference,
            "model provenance",
        ),
        (
            candidate.model_license_ref,
            descriptor.license_reference,
            "model license",
        ),
        (candidate.model_sha256, descriptor.sha256, "model digest"),
    )
    for actual, trusted, label in expected:
        if actual != trusted:
            raise PhysicalCandidateEvaluationError(
                f"candidate {label} does not match the physical artifact descriptor"
            )


def _canonical_path(value: str | os.PathLike[str], *, name: str) -> str:
    if isinstance(value, bytes):
        raise TypeError(f"{name} must be text or a text path-like object")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{name} must be absolute")
    return os.fspath(path)


class PhysicalCandidateGateway:
    """Bind Model Lab inference to repeated #825 candidate-byte verification.

    The wrapper does not claim that a provider's internal cache/load is byte-identical
    to the verified path. It proves only that the canonical candidate descriptor and
    physical file still agree immediately before each provider completion dispatch,
    while also preventing provider/model/fallback substitution at this boundary.
    Provider-specific byte-load attestation remains a separate acceptance requirement.
    """

    def __init__(
        self,
        gateway: ModelCompletionPort,
        *,
        candidate: ModelCandidate,
        descriptor: ModelArtifactDescriptor,
        artifact_path: str | os.PathLike[str],
        allowed_root: str | os.PathLike[str] | None = None,
    ) -> None:
        _validate_candidate_binding(candidate, descriptor)
        self._gateway = gateway
        self._candidate_id = candidate.candidate_id
        self._candidate_evidence_sha256 = candidate.evidence_sha256
        self._provider_id = candidate.provider_id
        self._provider_kind = candidate.provider_kind
        self._model = candidate.request_model
        self._descriptor = descriptor
        self._descriptor_digest = descriptor.descriptor_digest
        self._artifact_sha256 = candidate.model_sha256
        assert self._artifact_sha256 is not None
        self._artifact_path = _canonical_path(artifact_path, name="artifact_path")
        self._allowed_root = (
            None
            if allowed_root is None
            else _canonical_path(allowed_root, name="allowed_root")
        )
        self._verify_physical_binding()

    @property
    def candidate_evidence_sha256(self) -> str:
        return self._candidate_evidence_sha256

    @property
    def descriptor_digest(self) -> str:
        return self._descriptor_digest

    def _verify_physical_binding(self) -> None:
        _validate_descriptor(self._descriptor)
        if self._descriptor.descriptor_digest != self._descriptor_digest:
            raise PhysicalCandidateEvaluationError(
                "candidate artifact descriptor changed after evaluation binding"
            )
        receipt = verify_candidate_artifact(
            self._artifact_path,
            self._descriptor,
            allowed_root=self._allowed_root,
        )
        if (
            receipt.descriptor_digest != self._descriptor_digest
            or receipt.sha256 != self._artifact_sha256
        ):
            raise PhysicalCandidateEvaluationError(
                "candidate physical verification does not match evaluation binding"
            )

    def _validate_benchmark_evidence(self, request: ModelRequest) -> None:
        metadata = request.metadata
        required = (
            "benchmark_configuration_sha256",
            "benchmark_execution_config_sha256",
            "evaluation_set_id",
            "evaluation_set_sha256",
            "evaluation_set_version",
            "model_candidate_id",
        )
        if any(key not in metadata for key in required):
            raise PhysicalCandidateEvaluationError(
                "benchmark request is missing candidate/evaluation evidence"
            )
        if metadata["model_candidate_id"] != self._candidate_id:
            raise PhysicalCandidateEvaluationError(
                "benchmark request candidate identity does not match the bound candidate"
            )
        try:
            expected_configuration = benchmark_configuration_sha256(
                candidate_evidence_sha256=self._candidate_evidence_sha256,
                evaluation_set_id=metadata["evaluation_set_id"],
                evaluation_set_version=metadata["evaluation_set_version"],
                evaluation_set_sha256=metadata["evaluation_set_sha256"],
                execution_config_sha256=metadata[
                    "benchmark_execution_config_sha256"
                ],
            )
        except (TypeError, ValueError) as exc:
            raise PhysicalCandidateEvaluationError(
                "benchmark request evidence metadata is invalid"
            ) from exc
        if metadata["benchmark_configuration_sha256"] != expected_configuration:
            raise PhysicalCandidateEvaluationError(
                "benchmark request configuration does not match the bound candidate"
            )

    def _validate_request(self, request: ModelRequest) -> None:
        if type(request) is not ModelRequest:
            raise TypeError("request must be an exact ModelRequest")
        ModelRequest.__post_init__(request)
        if request.provider_id != self._provider_id:
            raise PhysicalCandidateEvaluationError(
                "benchmark request provider does not match the bound candidate"
            )
        if request.provider_kind is not self._provider_kind:
            raise PhysicalCandidateEvaluationError(
                "benchmark request provider kind does not match the bound candidate"
            )
        if request.model != self._model:
            raise PhysicalCandidateEvaluationError(
                "benchmark request model does not match the bound candidate"
            )
        if request.fallback_provider_ids:
            raise PhysicalCandidateEvaluationError(
                "physical candidate evaluation forbids fallback provider substitution"
            )
        self._validate_benchmark_evidence(request)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self._validate_request(request)
        # Keep physical verification as the final Nika-owned check before provider effect.
        self._verify_physical_binding()
        return await self._gateway.complete(request)
