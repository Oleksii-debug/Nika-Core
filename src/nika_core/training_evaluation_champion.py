from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from nika_core.model_artifacts import (
    ModelArtifactDescriptor,
    ModelArtifactKind,
    ModelArtifactRegistryError,
    ModelIntegrityBasis,
)
from nika_core.model_engineering.contracts import (
    ModelCandidate,
    validate_model_candidate,
)
from nika_core.model_gateway.contracts import ProviderKind
from nika_core.training_artifacts import (
    CandidateArtifactIntegrityError,
    verify_candidate_artifact,
)
from nika_core.training_evaluation_binding import TrainingEvaluationBinding

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_IDENTITY_BYTES = 512


class ChampionEvaluationBindingError(RuntimeError):
    """Safe failure while binding the pre-training champion to held-out evaluation."""


def _identity(value: object, *, name: str) -> str:
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


def _sha256(value: object, *, name: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be an exact lowercase SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True, init=False)
class ChampionEvaluationBinding:
    """Exact physical champion authority for the frozen Loop-C held-out suite.

    The incumbent attested gateway/evaluator consume a structural binding surface
    named for the challenger. Read-only aliases below deliberately expose that
    existing surface so the same security authority can evaluate the champion
    without fabricating a second provider/evaluator path.
    """

    job_id: str
    candidate_id: str
    provider_id: str
    model_id: str
    artifact_sha256: str
    artifact_size_bytes: int
    frozen_package_sha256: str
    evaluation_set_sha256: str
    descriptor_digest: str
    descriptor_registry_key: str
    training_binding_sha256: str

    def __init_subclass__(cls, **_: object) -> None:
        raise TypeError("ChampionEvaluationBinding cannot be subclassed")

    def _validate(self) -> None:
        for value, name in (
            (self.job_id, "job_id"),
            (self.candidate_id, "candidate_id"),
            (self.provider_id, "provider_id"),
            (self.model_id, "model_id"),
        ):
            _identity(value, name=name)
        for value, name in (
            (self.artifact_sha256, "artifact_sha256"),
            (self.frozen_package_sha256, "frozen_package_sha256"),
            (self.evaluation_set_sha256, "evaluation_set_sha256"),
            (self.descriptor_digest, "descriptor_digest"),
            (self.descriptor_registry_key, "descriptor_registry_key"),
            (self.training_binding_sha256, "training_binding_sha256"),
        ):
            _sha256(value, name=name)
        if (
            type(self.artifact_size_bytes) is not int
            or self.artifact_size_bytes <= 0
        ):
            raise ValueError("artifact_size_bytes must be a positive integer")

    def revalidated(self) -> ChampionEvaluationBinding:
        if type(self) is not ChampionEvaluationBinding:
            raise TypeError("binding must be an exact ChampionEvaluationBinding")
        try:
            self._validate()
            return _build_binding(
                job_id=self.job_id,
                candidate_id=self.candidate_id,
                provider_id=self.provider_id,
                model_id=self.model_id,
                artifact_sha256=self.artifact_sha256,
                artifact_size_bytes=self.artifact_size_bytes,
                frozen_package_sha256=self.frozen_package_sha256,
                evaluation_set_sha256=self.evaluation_set_sha256,
                descriptor_digest=self.descriptor_digest,
                descriptor_registry_key=self.descriptor_registry_key,
                training_binding_sha256=self.training_binding_sha256,
            )
        except AttributeError as exc:
            raise ValueError("champion evaluation binding fields are incomplete") from exc

    def evidence_payload(self) -> dict[str, object]:
        binding = self.revalidated()
        return {
            "schema": "nika-champion-evaluation-binding-v1",
            "job_id": binding.job_id,
            "candidate_id": binding.candidate_id,
            "provider_id": binding.provider_id,
            "model_id": binding.model_id,
            "artifact_sha256": binding.artifact_sha256,
            "artifact_size_bytes": binding.artifact_size_bytes,
            "frozen_package_sha256": binding.frozen_package_sha256,
            "evaluation_set_sha256": binding.evaluation_set_sha256,
            "descriptor_digest": binding.descriptor_digest,
            "descriptor_registry_key": binding.descriptor_registry_key,
            "training_binding_sha256": binding.training_binding_sha256,
        }

    @property
    def binding_sha256(self) -> str:
        encoded = json.dumps(
            self.evidence_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def challenger_candidate_id(self) -> str:
        return self.candidate_id

    @property
    def challenger_provider_id(self) -> str:
        return self.provider_id

    @property
    def challenger_model_id(self) -> str:
        return self.model_id

    @property
    def challenger_sha256(self) -> str:
        return self.artifact_sha256

    @property
    def challenger_size_bytes(self) -> int:
        return self.artifact_size_bytes


def _build_binding(
    *,
    job_id: str,
    candidate_id: str,
    provider_id: str,
    model_id: str,
    artifact_sha256: str,
    artifact_size_bytes: int,
    frozen_package_sha256: str,
    evaluation_set_sha256: str,
    descriptor_digest: str,
    descriptor_registry_key: str,
    training_binding_sha256: str,
) -> ChampionEvaluationBinding:
    result = object.__new__(ChampionEvaluationBinding)
    for name, value in (
        ("job_id", job_id),
        ("candidate_id", candidate_id),
        ("provider_id", provider_id),
        ("model_id", model_id),
        ("artifact_sha256", artifact_sha256),
        ("artifact_size_bytes", artifact_size_bytes),
        ("frozen_package_sha256", frozen_package_sha256),
        ("evaluation_set_sha256", evaluation_set_sha256),
        ("descriptor_digest", descriptor_digest),
        ("descriptor_registry_key", descriptor_registry_key),
        ("training_binding_sha256", training_binding_sha256),
    ):
        object.__setattr__(result, name, value)
    result._validate()
    return result


def _snapshot_training_binding(
    binding: TrainingEvaluationBinding,
) -> TrainingEvaluationBinding:
    if type(binding) is not TrainingEvaluationBinding:
        raise TypeError("training_binding must be an exact TrainingEvaluationBinding")
    try:
        return binding.revalidated()
    except (AttributeError, TypeError, ValueError) as exc:
        raise ChampionEvaluationBindingError(
            "training evaluation binding is not canonical"
        ) from exc


def _snapshot_champion(champion: ModelCandidate) -> ModelCandidate:
    if type(champion) is not ModelCandidate:
        raise TypeError("champion must be an exact ModelCandidate")
    try:
        validate_model_candidate(champion)
        return ModelCandidate(
            candidate_id=champion.candidate_id,
            provider_id=champion.provider_id,
            provider_kind=champion.provider_kind,
            request_model=champion.request_model,
            expected_response_model=champion.expected_response_model,
            engine_provenance_ref=champion.engine_provenance_ref,
            engine_license_ref=champion.engine_license_ref,
            model_provenance_ref=champion.model_provenance_ref,
            model_license_ref=champion.model_license_ref,
            model_sha256=champion.model_sha256,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ChampionEvaluationBindingError(
            "champion Model-Lab candidate is not canonical"
        ) from exc


def _snapshot_descriptor(
    descriptor: ModelArtifactDescriptor,
) -> ModelArtifactDescriptor:
    if type(descriptor) is not ModelArtifactDescriptor:
        raise TypeError("descriptor must be an exact ModelArtifactDescriptor")
    try:
        return ModelArtifactDescriptor.from_json(descriptor.canonical_json())
    except (AttributeError, ModelArtifactRegistryError, TypeError, ValueError) as exc:
        raise ChampionEvaluationBindingError(
            "champion model descriptor is not canonical"
        ) from exc


def bind_champion_for_attested_evaluation(
    *,
    training_binding: TrainingEvaluationBinding,
    champion: ModelCandidate,
    descriptor: ModelArtifactDescriptor,
    champion_path: str | Path,
    allowed_root: str | Path | None = None,
) -> ChampionEvaluationBinding:
    """Bind the exact physical pre-training champion to Loop-C held-out authority.

    This does not execute inference and does not promote a model. It only proves
    that the champion identity already bound as the training base artifact has a
    local SHA-256 descriptor and that the supplied physical bytes still match it.
    """

    training = _snapshot_training_binding(training_binding)
    candidate = _snapshot_champion(champion)
    model_descriptor = _snapshot_descriptor(descriptor)

    if candidate.candidate_id != training.base_candidate_id:
        raise ChampionEvaluationBindingError(
            "champion identity does not match the training base candidate"
        )
    if candidate.provider_kind is not ProviderKind.LOCAL:
        raise ChampionEvaluationBindingError(
            "physically attested champion requires the local provider boundary"
        )
    if candidate.model_sha256 != training.base_sha256:
        raise ChampionEvaluationBindingError(
            "champion digest does not match the training base artifact"
        )
    if (
        candidate.provider_id != model_descriptor.provider_id
        or candidate.request_model != model_descriptor.model_id
        or candidate.expected_response_model != model_descriptor.model_id
    ):
        raise ChampionEvaluationBindingError(
            "champion provider/model route does not match its model descriptor"
        )
    if (
        candidate.model_provenance_ref != model_descriptor.source_reference
        or candidate.model_license_ref != model_descriptor.license_reference
    ):
        raise ChampionEvaluationBindingError(
            "champion provenance does not match its model descriptor"
        )
    if model_descriptor.kind not in {
        ModelArtifactKind.EMBEDDED,
        ModelArtifactKind.EXTERNAL_LOCAL,
    }:
        raise ChampionEvaluationBindingError(
            "champion descriptor must represent a local model artifact"
        )
    if model_descriptor.integrity_basis is not ModelIntegrityBasis.SHA256:
        raise ChampionEvaluationBindingError(
            "champion descriptor requires SHA-256 integrity"
        )
    if (
        model_descriptor.sha256 != training.base_sha256
        or model_descriptor.size_bytes is None
    ):
        raise ChampionEvaluationBindingError(
            "champion descriptor does not match the training base bytes"
        )

    if (
        candidate.provider_id != training.base_provider_id
        or candidate.request_model != training.base_model_id
    ):
        raise ChampionEvaluationBindingError(
            "champion route does not match the training-bound base authority"
        )
    if (
        model_descriptor.descriptor_digest != training.base_descriptor_digest
        or model_descriptor.registry_key != training.base_descriptor_registry_key
        or model_descriptor.size_bytes != training.base_size_bytes
    ):
        raise ChampionEvaluationBindingError(
            "champion descriptor does not match the training-bound base descriptor"
        )

    try:
        receipt = verify_candidate_artifact(
            champion_path,
            model_descriptor,
            allowed_root=allowed_root,
        )
    except (CandidateArtifactIntegrityError, TypeError, ValueError) as exc:
        raise ChampionEvaluationBindingError(
            "champion physical artifact verification failed"
        ) from exc

    if (
        receipt.sha256 != training.base_sha256
        or receipt.size_bytes != model_descriptor.size_bytes
        or receipt.descriptor_digest != model_descriptor.descriptor_digest
        or receipt.registry_key != model_descriptor.registry_key
    ):
        raise ChampionEvaluationBindingError(
            "champion physical verification evidence is inconsistent"
        )

    return _build_binding(
        job_id=training.job_id,
        candidate_id=candidate.candidate_id,
        provider_id=candidate.provider_id,
        model_id=candidate.request_model,
        artifact_sha256=training.base_sha256,
        artifact_size_bytes=model_descriptor.size_bytes,
        frozen_package_sha256=training.frozen_package_sha256,
        evaluation_set_sha256=training.evaluation_set_sha256,
        descriptor_digest=model_descriptor.descriptor_digest,
        descriptor_registry_key=model_descriptor.registry_key,
        training_binding_sha256=training.binding_sha256,
    )


__all__ = [
    "ChampionEvaluationBinding",
    "ChampionEvaluationBindingError",
    "bind_champion_for_attested_evaluation",
]
