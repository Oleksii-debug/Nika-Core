from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from nika_core.learning_package import FrozenLearningPackage, LearningPackageError
from nika_core.model_artifacts import (
    ModelArtifactDescriptor,
    ModelArtifactKind,
    ModelArtifactRegistryError,
    ModelIntegrityBasis,
)
from nika_core.model_engineering.contracts import (
    EvaluationPurpose,
    EvaluationSet,
    ModelCandidate,
    validate_evaluation_set,
    validate_model_candidate,
)
from nika_core.model_gateway.contracts import ProviderKind
from nika_core.training_artifacts import (
    CandidateArtifactIntegrityError,
    verify_candidate_artifact,
)
from nika_core.training_runtime import (
    ArtifactIdentity,
    TrainingJobSpec,
    TrainingRunEvidence,
    TrainingRunState,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_IDENTITY_BYTES = 512


def _validated_identity_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty canonical text")
    if any(not character.isprintable() for character in value):
        raise ValueError(f"{name} must not contain control characters")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8 text") from exc
    if len(encoded) > _MAX_IDENTITY_BYTES:
        raise ValueError(f"{name} exceeds the configured byte limit")
    return value


class TrainingEvaluationBindingError(RuntimeError):
    """Safe failure while binding a completed training result to evaluation evidence."""


@dataclass(frozen=True, slots=True)
class TrainingEvaluationBinding:
    """Secret-free identity proof prepared before any old/new benchmark effect.

    This receipt proves that the completed Loop-C run, frozen package, physical
    challenger artifact, canonical model descriptor, Model-Lab candidate identities,
    and held-out evaluation set agree. It does not prove that a model provider later
    loads these exact bytes. The provider/evaluator boundary must separately attest
    the actually loaded artifact before promotion can be authorized.
    """

    job_id: str
    base_candidate_id: str
    base_provider_id: str
    base_model_id: str
    challenger_candidate_id: str
    challenger_provider_id: str
    challenger_model_id: str
    base_sha256: str
    challenger_sha256: str
    candidate_artifact_ref: str
    frozen_package_sha256: str
    evaluation_set_sha256: str
    base_descriptor_digest: str
    base_descriptor_registry_key: str
    base_size_bytes: int
    descriptor_digest: str
    descriptor_registry_key: str
    challenger_size_bytes: int

    def __post_init__(self) -> None:
        for value, name in (
            (self.job_id, "job_id"),
            (self.base_candidate_id, "base_candidate_id"),
            (self.base_provider_id, "base_provider_id"),
            (self.base_model_id, "base_model_id"),
            (self.challenger_candidate_id, "challenger_candidate_id"),
            (self.challenger_provider_id, "challenger_provider_id"),
            (self.challenger_model_id, "challenger_model_id"),
            (self.candidate_artifact_ref, "candidate_artifact_ref"),
        ):
            _validated_identity_text(value, name=name)
        for value, name in (
            (self.base_sha256, "base_sha256"),
            (self.challenger_sha256, "challenger_sha256"),
            (self.frozen_package_sha256, "frozen_package_sha256"),
            (self.evaluation_set_sha256, "evaluation_set_sha256"),
            (self.base_descriptor_digest, "base_descriptor_digest"),
            (self.base_descriptor_registry_key, "base_descriptor_registry_key"),
            (self.descriptor_digest, "descriptor_digest"),
            (self.descriptor_registry_key, "descriptor_registry_key"),
        ):
            if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
                raise ValueError(f"{name} must be an exact lowercase SHA-256 digest")
        if type(self.base_size_bytes) is not int or self.base_size_bytes <= 0:
            raise ValueError("base_size_bytes must be a positive integer")
        if (
            type(self.challenger_size_bytes) is not int
            or self.challenger_size_bytes <= 0
        ):
            raise ValueError("challenger_size_bytes must be a positive integer")
        if self.base_candidate_id == self.challenger_candidate_id:
            raise ValueError("old/new candidate identities must be distinct")

    def revalidated(self) -> TrainingEvaluationBinding:
        if type(self) is not TrainingEvaluationBinding:
            raise TypeError("binding must be an exact TrainingEvaluationBinding")
        try:
            return TrainingEvaluationBinding(
                job_id=self.job_id,
                base_candidate_id=self.base_candidate_id,
                base_provider_id=self.base_provider_id,
                base_model_id=self.base_model_id,
                challenger_candidate_id=self.challenger_candidate_id,
                challenger_provider_id=self.challenger_provider_id,
                challenger_model_id=self.challenger_model_id,
                base_sha256=self.base_sha256,
                challenger_sha256=self.challenger_sha256,
                candidate_artifact_ref=self.candidate_artifact_ref,
                frozen_package_sha256=self.frozen_package_sha256,
                evaluation_set_sha256=self.evaluation_set_sha256,
                base_descriptor_digest=self.base_descriptor_digest,
                base_descriptor_registry_key=self.base_descriptor_registry_key,
                base_size_bytes=self.base_size_bytes,
                descriptor_digest=self.descriptor_digest,
                descriptor_registry_key=self.descriptor_registry_key,
                challenger_size_bytes=self.challenger_size_bytes,
            )
        except AttributeError as exc:
            raise ValueError("binding fields must be complete") from exc

    def _binding_sha256_unchecked(self) -> str:
        payload = {
            "schema": "nika-training-evaluation-binding-v2",
            "job_id": self.job_id,
            "base_candidate_id": self.base_candidate_id,
            "base_provider_id": self.base_provider_id,
            "base_model_id": self.base_model_id,
            "challenger_candidate_id": self.challenger_candidate_id,
            "challenger_provider_id": self.challenger_provider_id,
            "challenger_model_id": self.challenger_model_id,
            "base_sha256": self.base_sha256,
            "challenger_sha256": self.challenger_sha256,
            "candidate_artifact_ref": self.candidate_artifact_ref,
            "frozen_package_sha256": self.frozen_package_sha256,
            "evaluation_set_sha256": self.evaluation_set_sha256,
            "base_descriptor_digest": self.base_descriptor_digest,
            "base_descriptor_registry_key": self.base_descriptor_registry_key,
            "base_size_bytes": self.base_size_bytes,
            "descriptor_digest": self.descriptor_digest,
            "descriptor_registry_key": self.descriptor_registry_key,
            "challenger_size_bytes": self.challenger_size_bytes,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def binding_sha256(self) -> str:
        return self.revalidated()._binding_sha256_unchecked()


def _require_sha256(value: object, *, name: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise TrainingEvaluationBindingError(
            f"{name} must be an exact lowercase SHA-256 digest"
        )
    return value


def _require_identity_text(value: object, *, name: str) -> str:
    try:
        return _validated_identity_text(value, name=name)
    except ValueError as exc:
        raise TrainingEvaluationBindingError(str(exc)) from exc


def _snapshot_spec(spec: TrainingJobSpec) -> TrainingJobSpec:
    if type(spec) is not TrainingJobSpec:
        raise TypeError("spec must be an exact TrainingJobSpec")
    try:
        if type(spec.base_artifact) is not ArtifactIdentity:
            raise TypeError("base_artifact must be an exact ArtifactIdentity")
        base = ArtifactIdentity(
            artifact_ref=spec.base_artifact.artifact_ref,
            sha256=spec.base_artifact.sha256,
        )
        return TrainingJobSpec(
            job_id=spec.job_id,
            task_id=spec.task_id,
            project_id=spec.project_id,
            owner_id=spec.owner_id,
            base_artifact=base,
            frozen_package_sha256=spec.frozen_package_sha256,
            training_material_sha256=spec.training_material_sha256,
            candidate_artifact_ref=spec.candidate_artifact_ref,
            max_steps=spec.max_steps,
            resource_scope=spec.resource_scope,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise TrainingEvaluationBindingError(
            "training job specification is not canonical"
        ) from exc


def _snapshot_package(
    package: FrozenLearningPackage,
    *,
    expected_manifest_sha256: str,
) -> FrozenLearningPackage:
    if type(package) is not FrozenLearningPackage:
        raise TypeError("package must be an exact FrozenLearningPackage")
    try:
        serialized = package.to_json()
        return FrozenLearningPackage.from_json(
            serialized,
            expected_manifest_sha256=expected_manifest_sha256,
        )
    except (AttributeError, LearningPackageError, TypeError, ValueError) as exc:
        raise TrainingEvaluationBindingError(
            "frozen learning package is not canonical"
        ) from exc


def _snapshot_descriptor(
    descriptor: ModelArtifactDescriptor,
    *,
    role: str,
) -> ModelArtifactDescriptor:
    if type(descriptor) is not ModelArtifactDescriptor:
        raise TypeError("descriptor must be an exact ModelArtifactDescriptor")
    if role not in {"base", "challenger"}:
        raise ValueError("descriptor role is invalid")
    try:
        return ModelArtifactDescriptor.from_json(descriptor.canonical_json())
    except (AttributeError, ModelArtifactRegistryError, TypeError, ValueError) as exc:
        raise TrainingEvaluationBindingError(
            f"{role} model descriptor is not canonical"
        ) from exc


def _validate_completed_run(
    evidence: TrainingRunEvidence,
    *,
    spec: TrainingJobSpec,
) -> str:
    if type(evidence) is not TrainingRunEvidence:
        raise TypeError("evidence must be an exact TrainingRunEvidence")
    try:
        base_artifact = evidence.base_artifact
        if type(base_artifact) is not ArtifactIdentity:
            raise TypeError("run base_artifact must be an exact ArtifactIdentity")
        observed_base = ArtifactIdentity(
            artifact_ref=base_artifact.artifact_ref,
            sha256=base_artifact.sha256,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise TrainingEvaluationBindingError(
            "training run base artifact evidence is not canonical"
        ) from exc

    if evidence.state is not TrainingRunState.COMPLETED:
        raise TrainingEvaluationBindingError(
            "only completed training may be bound for old/new evaluation"
        )
    if type(evidence.next_step) is not int or not 1 <= evidence.next_step <= spec.max_steps:
        raise TrainingEvaluationBindingError(
            "completed training carries an invalid next-step boundary"
        )

    observed_job_id = _require_identity_text(evidence.job_id, name="training run job_id")
    observed_package_sha256 = _require_sha256(
        evidence.frozen_package_sha256,
        name="training run frozen package SHA-256",
    )
    observed_material_sha256 = _require_sha256(
        evidence.training_material_sha256,
        name="training run material SHA-256",
    )
    observed_candidate_ref = _require_identity_text(
        evidence.candidate_artifact_ref,
        name="training run candidate artifact ref",
    )
    candidate_sha256 = _require_sha256(
        evidence.candidate_sha256,
        name="training candidate SHA-256",
    )
    if (
        observed_job_id != spec.job_id
        or observed_base != spec.base_artifact
        or observed_package_sha256 != spec.frozen_package_sha256
        or observed_material_sha256 != spec.training_material_sha256
        or observed_candidate_ref != spec.candidate_artifact_ref
    ):
        raise TrainingEvaluationBindingError(
            "training run evidence does not match the exact training job"
        )
    return candidate_sha256


def _validate_evaluation_set(
    evaluation_set: EvaluationSet,
    *,
    expected_sha256: str,
) -> None:
    try:
        validate_evaluation_set(evaluation_set)
    except (AttributeError, TypeError, ValueError) as exc:
        raise TrainingEvaluationBindingError(
            "evaluation set is not canonical"
        ) from exc
    if evaluation_set.purpose is not EvaluationPurpose.HELD_OUT:
        raise TrainingEvaluationBindingError(
            "Loop-C old/new evaluation requires a held-out evaluation set"
        )
    if evaluation_set.content_sha256 != expected_sha256:
        raise TrainingEvaluationBindingError(
            "evaluation set does not match the frozen learning package"
        )


def _validate_champion(
    champion: ModelCandidate,
    *,
    spec: TrainingJobSpec,
    descriptor: ModelArtifactDescriptor,
) -> None:
    try:
        validate_model_candidate(champion)
    except (AttributeError, TypeError, ValueError) as exc:
        raise TrainingEvaluationBindingError(
            "base Model-Lab candidate is not canonical"
        ) from exc
    if champion.candidate_id != spec.base_artifact.artifact_ref:
        raise TrainingEvaluationBindingError(
            "base Model-Lab candidate identity does not match training base artifact"
        )
    if champion.provider_kind is not ProviderKind.LOCAL:
        raise TrainingEvaluationBindingError(
            "physical base candidate must use the local provider boundary"
        )
    if (
        champion.provider_id != descriptor.provider_id
        or champion.request_model != descriptor.model_id
        or champion.expected_response_model != descriptor.model_id
    ):
        raise TrainingEvaluationBindingError(
            "base provider/model route does not match its model descriptor"
        )
    if champion.model_sha256 != spec.base_artifact.sha256:
        raise TrainingEvaluationBindingError(
            "base Model-Lab candidate digest does not match training base artifact"
        )
    if (
        champion.model_provenance_ref != descriptor.source_reference
        or champion.model_license_ref != descriptor.license_reference
    ):
        raise TrainingEvaluationBindingError(
            "base provenance does not match its model descriptor"
        )


def _validate_challenger(
    challenger: ModelCandidate,
    *,
    spec: TrainingJobSpec,
    descriptor: ModelArtifactDescriptor,
    candidate_sha256: str,
) -> None:
    try:
        validate_model_candidate(challenger)
    except (AttributeError, TypeError, ValueError) as exc:
        raise TrainingEvaluationBindingError(
            "challenger Model-Lab candidate is not canonical"
        ) from exc

    if challenger.candidate_id != spec.candidate_artifact_ref:
        raise TrainingEvaluationBindingError(
            "challenger candidate identity does not match training output reference"
        )
    if challenger.provider_kind is not ProviderKind.LOCAL:
        raise TrainingEvaluationBindingError(
            "physically trained challenger must use the local provider boundary"
        )
    if (
        challenger.provider_id != descriptor.provider_id
        or challenger.request_model != descriptor.model_id
        or challenger.expected_response_model != descriptor.model_id
    ):
        raise TrainingEvaluationBindingError(
            "challenger provider/model route does not match its model descriptor"
        )
    if challenger.model_sha256 != candidate_sha256:
        raise TrainingEvaluationBindingError(
            "challenger Model-Lab digest does not match completed training"
        )
    if (
        challenger.model_provenance_ref != descriptor.source_reference
        or challenger.model_license_ref != descriptor.license_reference
    ):
        raise TrainingEvaluationBindingError(
            "challenger provenance does not match its model descriptor"
        )


def bind_training_result_for_evaluation(
    *,
    spec: TrainingJobSpec,
    evidence: TrainingRunEvidence,
    package: FrozenLearningPackage,
    base_path: str | Path,
    base_descriptor: ModelArtifactDescriptor,
    candidate_path: str | Path,
    descriptor: ModelArtifactDescriptor,
    champion: ModelCandidate,
    challenger: ModelCandidate,
    evaluation_set: EvaluationSet,
    allowed_root: str | Path | None = None,
) -> TrainingEvaluationBinding:
    """Bind completed training to exact old/new evaluation identities.

    No benchmark or provider effect occurs here. Physical bytes are reverified at this
    binding boundary, but this receipt deliberately does not claim those exact bytes
    were loaded by the later ModelGateway/provider. Promotion code must require a
    separate loaded-artifact attestation at the evaluator effect boundary.
    """

    canonical_spec = _snapshot_spec(spec)
    canonical_package = _snapshot_package(
        package,
        expected_manifest_sha256=canonical_spec.frozen_package_sha256,
    )
    if canonical_package.base_artifact_sha256 != canonical_spec.base_artifact.sha256:
        raise TrainingEvaluationBindingError(
            "frozen package base artifact does not match training job"
        )

    candidate_sha256 = _validate_completed_run(
        evidence,
        spec=canonical_spec,
    )
    canonical_base_descriptor = _snapshot_descriptor(
        base_descriptor,
        role="base",
    )
    canonical_descriptor = _snapshot_descriptor(
        descriptor,
        role="challenger",
    )
    if canonical_base_descriptor.kind not in {
        ModelArtifactKind.EMBEDDED,
        ModelArtifactKind.EXTERNAL_LOCAL,
    }:
        raise TrainingEvaluationBindingError(
            "base descriptor must represent a local model artifact"
        )
    if canonical_base_descriptor.integrity_basis is not ModelIntegrityBasis.SHA256:
        raise TrainingEvaluationBindingError(
            "base descriptor requires SHA-256 integrity"
        )
    if (
        canonical_base_descriptor.sha256 != canonical_spec.base_artifact.sha256
        or canonical_base_descriptor.size_bytes is None
    ):
        raise TrainingEvaluationBindingError(
            "base descriptor does not match training base bytes"
        )
    if canonical_descriptor.kind not in {
        ModelArtifactKind.EMBEDDED,
        ModelArtifactKind.EXTERNAL_LOCAL,
    }:
        raise TrainingEvaluationBindingError(
            "trained challenger descriptor must represent a local model artifact"
        )
    if canonical_descriptor.integrity_basis is not ModelIntegrityBasis.SHA256:
        raise TrainingEvaluationBindingError(
            "trained challenger descriptor requires SHA-256 integrity"
        )
    if (
        canonical_descriptor.sha256 != candidate_sha256
        or canonical_descriptor.size_bytes is None
    ):
        raise TrainingEvaluationBindingError(
            "challenger descriptor does not match completed training bytes"
        )

    _validate_evaluation_set(
        evaluation_set,
        expected_sha256=canonical_package.evaluation_set_sha256,
    )
    _validate_champion(
        champion,
        spec=canonical_spec,
        descriptor=canonical_base_descriptor,
    )
    _validate_challenger(
        challenger,
        spec=canonical_spec,
        descriptor=canonical_descriptor,
        candidate_sha256=candidate_sha256,
    )
    if champion.candidate_id == challenger.candidate_id:
        raise TrainingEvaluationBindingError(
            "old/new evaluation requires distinct candidate identities"
        )

    try:
        base_receipt = verify_candidate_artifact(
            base_path,
            canonical_base_descriptor,
            allowed_root=allowed_root,
        )
    except (CandidateArtifactIntegrityError, TypeError, ValueError) as exc:
        raise TrainingEvaluationBindingError(
            "base physical artifact verification failed"
        ) from exc

    if (
        base_receipt.sha256 != canonical_spec.base_artifact.sha256
        or base_receipt.descriptor_digest
        != canonical_base_descriptor.descriptor_digest
        or base_receipt.registry_key != canonical_base_descriptor.registry_key
        or base_receipt.size_bytes != canonical_base_descriptor.size_bytes
    ):
        raise TrainingEvaluationBindingError(
            "base physical verification evidence is inconsistent"
        )

    try:
        receipt = verify_candidate_artifact(
            candidate_path,
            canonical_descriptor,
            allowed_root=allowed_root,
        )
    except (CandidateArtifactIntegrityError, TypeError, ValueError) as exc:
        raise TrainingEvaluationBindingError(
            "challenger physical artifact verification failed"
        ) from exc

    if (
        receipt.sha256 != candidate_sha256
        or receipt.descriptor_digest != canonical_descriptor.descriptor_digest
        or receipt.registry_key != canonical_descriptor.registry_key
        or receipt.size_bytes != canonical_descriptor.size_bytes
    ):
        raise TrainingEvaluationBindingError(
            "challenger physical verification evidence is inconsistent"
        )

    return TrainingEvaluationBinding(
        job_id=canonical_spec.job_id,
        base_candidate_id=champion.candidate_id,
        base_provider_id=champion.provider_id,
        base_model_id=champion.request_model,
        challenger_candidate_id=challenger.candidate_id,
        challenger_provider_id=challenger.provider_id,
        challenger_model_id=challenger.request_model,
        base_sha256=canonical_spec.base_artifact.sha256,
        challenger_sha256=candidate_sha256,
        candidate_artifact_ref=canonical_spec.candidate_artifact_ref,
        frozen_package_sha256=canonical_spec.frozen_package_sha256,
        evaluation_set_sha256=canonical_package.evaluation_set_sha256,
        base_descriptor_digest=canonical_base_descriptor.descriptor_digest,
        base_descriptor_registry_key=canonical_base_descriptor.registry_key,
        base_size_bytes=canonical_base_descriptor.size_bytes,
        descriptor_digest=canonical_descriptor.descriptor_digest,
        descriptor_registry_key=canonical_descriptor.registry_key,
        challenger_size_bytes=canonical_descriptor.size_bytes,
    )
