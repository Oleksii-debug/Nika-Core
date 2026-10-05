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
from nika_core.model_engineering import (
    AcceleratorObserverPort,
    CandidateBenchmarkReport,
    EvaluationPurpose,
    EvaluationSet,
    ModelCandidate,
    ModelScoringPort,
)
from nika_core.model_engineering.contracts import (
    validate_evaluation_set,
    validate_model_candidate,
)
from nika_core.model_gateway.contracts import ProviderKind
from nika_core.resources.contracts import ResourceObserverPort
from nika_core.training_artifacts import (
    CandidateArtifactIntegrityError,
    verify_candidate_artifact,
)
from nika_core.training_evaluation_attestation import LoadedModelAttestedCompletionPort
from nika_core.training_evaluation_binding import TrainingEvaluationBinding
from nika_core.training_evaluation_execution import (
    AttestedCaseReceipt,
    AttestedChallengerBenchmarkResult,
    run_attested_challenger_benchmark,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_IDENTITY_BYTES = 512


def _canonical_text(value: object, *, name: str) -> str:
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


@dataclass(frozen=True, slots=True)
class ChampionEvaluationBinding:
    """Physical base-model authority tied to one Loop-C training/evaluation binding."""

    training_binding_sha256: str
    job_id: str
    candidate_id: str
    provider_id: str
    model_id: str
    artifact_sha256: str
    evaluation_set_sha256: str
    descriptor_digest: str
    descriptor_registry_key: str
    artifact_size_bytes: int

    def __post_init__(self) -> None:
        for value, name in (
            (self.job_id, "job_id"),
            (self.candidate_id, "candidate_id"),
            (self.provider_id, "provider_id"),
            (self.model_id, "model_id"),
        ):
            _canonical_text(value, name=name)
        for value, name in (
            (self.training_binding_sha256, "training_binding_sha256"),
            (self.artifact_sha256, "artifact_sha256"),
            (self.evaluation_set_sha256, "evaluation_set_sha256"),
            (self.descriptor_digest, "descriptor_digest"),
            (self.descriptor_registry_key, "descriptor_registry_key"),
        ):
            _sha256(value, name=name)
        if type(self.artifact_size_bytes) is not int or self.artifact_size_bytes <= 0:
            raise ValueError("artifact_size_bytes must be a positive integer")

    def revalidated(self) -> ChampionEvaluationBinding:
        if type(self) is not ChampionEvaluationBinding:
            raise TypeError("binding must be an exact ChampionEvaluationBinding")
        try:
            return ChampionEvaluationBinding(
                training_binding_sha256=self.training_binding_sha256,
                job_id=self.job_id,
                candidate_id=self.candidate_id,
                provider_id=self.provider_id,
                model_id=self.model_id,
                artifact_sha256=self.artifact_sha256,
                evaluation_set_sha256=self.evaluation_set_sha256,
                descriptor_digest=self.descriptor_digest,
                descriptor_registry_key=self.descriptor_registry_key,
                artifact_size_bytes=self.artifact_size_bytes,
            )
        except AttributeError as exc:
            raise ValueError("champion binding fields are incomplete") from exc

    @property
    def binding_sha256(self) -> str:
        binding = self.revalidated()
        payload = {
            "schema": "nika-champion-evaluation-binding-v1",
            "training_binding_sha256": binding.training_binding_sha256,
            "job_id": binding.job_id,
            "candidate_id": binding.candidate_id,
            "provider_id": binding.provider_id,
            "model_id": binding.model_id,
            "artifact_sha256": binding.artifact_sha256,
            "evaluation_set_sha256": binding.evaluation_set_sha256,
            "descriptor_digest": binding.descriptor_digest,
            "descriptor_registry_key": binding.descriptor_registry_key,
            "artifact_size_bytes": binding.artifact_size_bytes,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def _snapshot_descriptor(descriptor: ModelArtifactDescriptor) -> ModelArtifactDescriptor:
    if type(descriptor) is not ModelArtifactDescriptor:
        raise TypeError("descriptor must be an exact ModelArtifactDescriptor")
    try:
        return ModelArtifactDescriptor.from_json(descriptor.canonical_json())
    except (AttributeError, ModelArtifactRegistryError, TypeError, ValueError) as exc:
        raise ValueError("champion descriptor must be canonical") from exc


def _snapshot_candidate(candidate: ModelCandidate) -> ModelCandidate:
    try:
        validate_model_candidate(candidate)
        return ModelCandidate(
            candidate_id=candidate.candidate_id,
            provider_id=candidate.provider_id,
            provider_kind=candidate.provider_kind,
            request_model=candidate.request_model,
            expected_response_model=candidate.expected_response_model,
            engine_provenance_ref=candidate.engine_provenance_ref,
            engine_license_ref=candidate.engine_license_ref,
            model_provenance_ref=candidate.model_provenance_ref,
            model_license_ref=candidate.model_license_ref,
            model_sha256=candidate.model_sha256,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("champion must be canonical") from exc


def bind_champion_artifact_for_evaluation(
    *,
    training_binding: TrainingEvaluationBinding,
    champion: ModelCandidate,
    descriptor: ModelArtifactDescriptor,
    candidate_path: str | Path,
    allowed_root: str | Path | None = None,
) -> ChampionEvaluationBinding:
    """Physically reverify the base model before any champion benchmark effect."""

    try:
        canonical_training = training_binding.revalidated()
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("training binding must be canonical") from exc
    canonical_champion = _snapshot_candidate(champion)
    canonical_descriptor = _snapshot_descriptor(descriptor)
    if canonical_champion.provider_kind is not ProviderKind.LOCAL:
        raise ValueError("champion physical artifact requires a local provider boundary")
    if canonical_descriptor.kind not in {
        ModelArtifactKind.EMBEDDED,
        ModelArtifactKind.EXTERNAL_LOCAL,
    }:
        raise ValueError("champion descriptor must represent a local model artifact")
    if canonical_descriptor.integrity_basis is not ModelIntegrityBasis.SHA256:
        raise ValueError("champion descriptor requires SHA-256 integrity")
    if canonical_descriptor.sha256 is None or canonical_descriptor.size_bytes is None:
        raise ValueError("champion descriptor requires exact digest and size")
    if (
        canonical_champion.candidate_id != canonical_training.base_candidate_id
        or canonical_champion.model_sha256 != canonical_training.base_sha256
        or canonical_descriptor.sha256 != canonical_training.base_sha256
    ):
        raise ValueError("champion artifact identity does not match training base authority")
    if (
        canonical_champion.provider_id != canonical_descriptor.provider_id
        or canonical_champion.request_model != canonical_descriptor.model_id
        or canonical_champion.expected_response_model != canonical_descriptor.model_id
    ):
        raise ValueError("champion route does not match its model descriptor")
    if (
        canonical_champion.model_provenance_ref != canonical_descriptor.source_reference
        or canonical_champion.model_license_ref != canonical_descriptor.license_reference
    ):
        raise ValueError("champion provenance does not match its model descriptor")
    try:
        receipt = verify_candidate_artifact(
            candidate_path,
            canonical_descriptor,
            allowed_root=allowed_root,
        )
    except (CandidateArtifactIntegrityError, TypeError, ValueError) as exc:
        raise ValueError("champion physical artifact verification failed") from exc
    if (
        receipt.sha256 != canonical_training.base_sha256
        or receipt.descriptor_digest != canonical_descriptor.descriptor_digest
        or receipt.registry_key != canonical_descriptor.registry_key
        or receipt.size_bytes != canonical_descriptor.size_bytes
    ):
        raise ValueError("champion physical verification evidence is inconsistent")
    return ChampionEvaluationBinding(
        training_binding_sha256=canonical_training.binding_sha256,
        job_id=canonical_training.job_id,
        candidate_id=canonical_champion.candidate_id,
        provider_id=canonical_champion.provider_id,
        model_id=canonical_champion.request_model,
        artifact_sha256=canonical_training.base_sha256,
        evaluation_set_sha256=canonical_training.evaluation_set_sha256,
        descriptor_digest=canonical_descriptor.descriptor_digest,
        descriptor_registry_key=canonical_descriptor.registry_key,
        artifact_size_bytes=canonical_descriptor.size_bytes,
    )


def _build_effect_binding(
    training_binding: TrainingEvaluationBinding,
    champion_binding: ChampionEvaluationBinding,
) -> TrainingEvaluationBinding:
    training = training_binding.revalidated()
    champion = champion_binding.revalidated()
    if champion.training_binding_sha256 != training.binding_sha256:
        raise ValueError("champion binding is not tied to the training evaluation authority")
    if champion.job_id != training.job_id:
        raise ValueError("champion binding job identity does not match training authority")
    if (
        champion.candidate_id != training.base_candidate_id
        or champion.artifact_sha256 != training.base_sha256
        or champion.evaluation_set_sha256 != training.evaluation_set_sha256
    ):
        raise ValueError("champion binding does not match training base authority")
    return TrainingEvaluationBinding(
        job_id=training.job_id,
        base_candidate_id=training.challenger_candidate_id,
        challenger_candidate_id=champion.candidate_id,
        challenger_provider_id=champion.provider_id,
        challenger_model_id=champion.model_id,
        base_sha256=training.challenger_sha256,
        challenger_sha256=champion.artifact_sha256,
        candidate_artifact_ref=champion.candidate_id,
        frozen_package_sha256=training.frozen_package_sha256,
        evaluation_set_sha256=training.evaluation_set_sha256,
        descriptor_digest=champion.descriptor_digest,
        descriptor_registry_key=champion.descriptor_registry_key,
        challenger_size_bytes=champion.artifact_size_bytes,
    )


@dataclass(frozen=True, slots=True, init=False)
class AttestedChampionBenchmarkResult:
    training_binding_sha256: str
    champion_binding: ChampionEvaluationBinding
    effect_binding_sha256: str
    benchmark: AttestedChallengerBenchmarkResult

    def __init_subclass__(cls, **_: object) -> None:
        raise TypeError("AttestedChampionBenchmarkResult cannot be subclassed")

    def _validate(self) -> None:
        _sha256(self.training_binding_sha256, name="training_binding_sha256")
        champion_binding = self.champion_binding.revalidated()
        if champion_binding.training_binding_sha256 != self.training_binding_sha256:
            raise ValueError("champion binding training authority changed")
        _sha256(self.effect_binding_sha256, name="effect_binding_sha256")
        benchmark = self.benchmark.revalidated()
        if benchmark.binding.binding_sha256 != self.effect_binding_sha256:
            raise ValueError("champion effect binding evidence changed")
        candidate = benchmark.report.candidate
        if (
            candidate.candidate_id != champion_binding.candidate_id
            or candidate.provider_id != champion_binding.provider_id
            or candidate.provider_kind is not ProviderKind.LOCAL
            or candidate.request_model != champion_binding.model_id
            or candidate.expected_response_model != champion_binding.model_id
            or candidate.model_sha256 != champion_binding.artifact_sha256
        ):
            raise ValueError("champion benchmark candidate does not match binding")
        if (
            benchmark.report.evaluation_purpose is not EvaluationPurpose.HELD_OUT
            or benchmark.report.evaluation_set_sha256
            != champion_binding.evaluation_set_sha256
        ):
            raise ValueError("champion benchmark evaluation does not match binding")

    def revalidated(self) -> AttestedChampionBenchmarkResult:
        if type(self) is not AttestedChampionBenchmarkResult:
            raise TypeError("result must be an exact AttestedChampionBenchmarkResult")
        try:
            self._validate()
            return _build_result(
                training_binding_sha256=self.training_binding_sha256,
                champion_binding=self.champion_binding.revalidated(),
                effect_binding_sha256=self.effect_binding_sha256,
                benchmark=self.benchmark.revalidated(),
            )
        except AttributeError as exc:
            raise ValueError("attested champion benchmark fields are incomplete") from exc

    @property
    def binding(self) -> ChampionEvaluationBinding:
        return self.revalidated().champion_binding

    @property
    def report(self) -> CandidateBenchmarkReport:
        return self.revalidated().benchmark.report

    @property
    def case_receipts(self) -> tuple[AttestedCaseReceipt, ...]:
        return self.revalidated().benchmark.case_receipts

    def evidence_payload(self) -> dict[str, object]:
        result = self.revalidated()
        benchmark = result.benchmark
        return {
            "schema": "nika-attested-champion-benchmark-v1",
            "training_binding_sha256": result.training_binding_sha256,
            "champion_binding_sha256": result.champion_binding.binding_sha256,
            "effect_binding_sha256": result.effect_binding_sha256,
            "job_id": result.champion_binding.job_id,
            "champion_candidate_id": result.champion_binding.candidate_id,
            "champion_sha256": result.champion_binding.artifact_sha256,
            "evaluation_set_sha256": result.champion_binding.evaluation_set_sha256,
            "execution_config_sha256": benchmark.report.execution_config_sha256,
            "benchmark_run_id": benchmark.report.run.run_id,
            "attested_benchmark_sha256": benchmark.evidence_sha256,
            "case_count": len(benchmark.case_receipts),
            "case_receipt_sha256": [
                receipt.evidence_sha256 for receipt in benchmark.case_receipts
            ],
        }

    @property
    def evidence_sha256(self) -> str:
        encoded = json.dumps(
            self.evidence_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def _build_result(
    *,
    training_binding_sha256: str,
    champion_binding: ChampionEvaluationBinding,
    effect_binding_sha256: str,
    benchmark: AttestedChallengerBenchmarkResult,
) -> AttestedChampionBenchmarkResult:
    result = object.__new__(AttestedChampionBenchmarkResult)
    object.__setattr__(result, "training_binding_sha256", training_binding_sha256)
    object.__setattr__(result, "champion_binding", champion_binding)
    object.__setattr__(result, "effect_binding_sha256", effect_binding_sha256)
    object.__setattr__(result, "benchmark", benchmark)
    result._validate()
    return result


async def run_attested_champion_benchmark(
    *,
    training_binding: TrainingEvaluationBinding,
    champion_binding: ChampionEvaluationBinding,
    champion: ModelCandidate,
    evaluation_set: EvaluationSet,
    effect_port: LoadedModelAttestedCompletionPort,
    expected_attestor_id: str,
    expected_attestor_sha256: str,
    timeout_seconds: float = 60.0,
    temperature: float | None = 0.0,
    scorer: ModelScoringPort | None = None,
    scorer_id: str | None = None,
    resource_observer: ResourceObserverPort | None = None,
    accelerator_observer: AcceleratorObserverPort | None = None,
) -> AttestedChampionBenchmarkResult:
    """Reuse the canonical attested benchmark executor for the exact physical champion."""

    try:
        training = training_binding.revalidated()
        physical = champion_binding.revalidated()
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("evaluation bindings must be canonical") from exc
    canonical_champion = _snapshot_candidate(champion)
    try:
        validate_evaluation_set(evaluation_set)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("evaluation set must be canonical") from exc
    if (
        canonical_champion.candidate_id != physical.candidate_id
        or canonical_champion.provider_id != physical.provider_id
        or canonical_champion.provider_kind is not ProviderKind.LOCAL
        or canonical_champion.request_model != physical.model_id
        or canonical_champion.expected_response_model != physical.model_id
        or canonical_champion.model_sha256 != physical.artifact_sha256
    ):
        raise ValueError("champion does not match physical champion binding")
    if evaluation_set.content_sha256 != physical.evaluation_set_sha256:
        raise ValueError("evaluation set does not match champion binding")
    if evaluation_set.purpose is not EvaluationPurpose.HELD_OUT:
        raise ValueError("Loop-C champion benchmark requires held-out evaluation")
    effect_binding = _build_effect_binding(training, physical)
    benchmark = await run_attested_challenger_benchmark(
        binding=effect_binding,
        challenger=canonical_champion,
        evaluation_set=evaluation_set,
        effect_port=effect_port,
        expected_attestor_id=expected_attestor_id,
        expected_attestor_sha256=expected_attestor_sha256,
        timeout_seconds=timeout_seconds,
        temperature=temperature,
        scorer=scorer,
        scorer_id=scorer_id,
        resource_observer=resource_observer,
        accelerator_observer=accelerator_observer,
    )
    return _build_result(
        training_binding_sha256=training.binding_sha256,
        champion_binding=physical,
        effect_binding_sha256=effect_binding.binding_sha256,
        benchmark=benchmark,
    )


__all__ = [
    "AttestedChampionBenchmarkResult",
    "ChampionEvaluationBinding",
    "bind_champion_artifact_for_evaluation",
    "run_attested_champion_benchmark",
]
