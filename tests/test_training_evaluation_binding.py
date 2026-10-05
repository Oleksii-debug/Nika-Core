from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from nika_core.learning_package import (
    FrozenLearningPackage,
    LearningDataSplit,
    LearningShard,
)
from nika_core.model_artifacts import (
    ModelArtifactDescriptor,
    ModelArtifactKind,
    ModelIntegrityBasis,
)
from nika_core.model_engineering import (
    EvaluationCase,
    EvaluationPurpose,
    EvaluationSet,
    ModelCandidate,
)
from nika_core.model_gateway.contracts import ModelMessage, PrivacyClass, ProviderKind
from nika_core.training_evaluation_binding import (
    TrainingEvaluationBindingError,
    bind_training_result_for_evaluation,
)
from nika_core.training_runtime import (
    ArtifactIdentity,
    TrainingJobSpec,
    TrainingRunEvidence,
    TrainingRunState,
)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _evaluation_set(*, expected_text: str = "4") -> EvaluationSet:
    return EvaluationSet(
        evaluation_set_id="held-out-arithmetic",
        version="1",
        provenance_ref="eval-provenance",
        license_ref="eval-license",
        purpose=EvaluationPurpose.HELD_OUT,
        privacy=PrivacyClass.PRIVATE,
        cases=(
            EvaluationCase(
                case_id="case-1",
                messages=(ModelMessage(role="user", content="2+2?"),),
                expected_text=expected_text,
            ),
        ),
    )


def _package(
    *,
    base_sha256: str,
    evaluation_set_sha256: str,
) -> FrozenLearningPackage:
    return FrozenLearningPackage.freeze(
        package_id="package-1",
        package_version="1",
        base_artifact_sha256=base_sha256,
        selection_policy_sha256=_sha(b"selection-policy"),
        verification_sha256=_sha(b"verification"),
        evaluation_set_sha256=evaluation_set_sha256,
        shards=(
            LearningShard(
                split=LearningDataSplit.TRAINING,
                artifact_sha256=_sha(b"training"),
                provenance_sha256=_sha(b"training-provenance"),
                license_evidence_sha256=_sha(b"training-license"),
                record_count=1,
                byte_count=len(b"training"),
            ),
            LearningShard(
                split=LearningDataSplit.VALIDATION,
                artifact_sha256=_sha(b"validation"),
                provenance_sha256=_sha(b"validation-provenance"),
                license_evidence_sha256=_sha(b"validation-license"),
                record_count=1,
                byte_count=len(b"validation"),
            ),
        ),
    )


def _fixture(tmp_path: Path) -> dict[str, object]:
    base_sha256 = _sha(b"base-model")
    candidate_bytes = b"candidate-model-weights"
    candidate_sha256 = _sha(candidate_bytes)
    candidate_path = tmp_path / "candidate.bin"
    candidate_path.write_bytes(candidate_bytes)

    evaluation_set = _evaluation_set()
    package = _package(
        base_sha256=base_sha256,
        evaluation_set_sha256=evaluation_set.content_sha256,
    )
    spec = TrainingJobSpec(
        job_id="job-1",
        task_id="task-1",
        project_id="project-1",
        owner_id="owner-1",
        base_artifact=ArtifactIdentity("models/base", base_sha256),
        frozen_package_sha256=package.manifest_sha256,
        training_material_sha256=_sha(b"resolved-training-material"),
        candidate_artifact_ref="models/candidate/job-1",
        max_steps=3,
    )
    evidence = TrainingRunEvidence(
        job_id=spec.job_id,
        state=TrainingRunState.COMPLETED,
        next_step=1,
        base_artifact=spec.base_artifact,
        frozen_package_sha256=spec.frozen_package_sha256,
        training_material_sha256=spec.training_material_sha256,
        candidate_artifact_ref=spec.candidate_artifact_ref,
        candidate_sha256=candidate_sha256,
        checkpoint_id="checkpoint-1",
    )
    descriptor = ModelArtifactDescriptor(
        kind=ModelArtifactKind.EXTERNAL_LOCAL,
        provider_id="ollama",
        model_id="candidate-model",
        model_version="job-1",
        source_reference="local-training:job-1",
        license_reference="license:project-1",
        integrity_basis=ModelIntegrityBasis.SHA256,
        sha256=candidate_sha256,
        size_bytes=len(candidate_bytes),
    )
    champion = ModelCandidate(
        candidate_id=spec.base_artifact.artifact_ref,
        provider_id="ollama",
        provider_kind=ProviderKind.LOCAL,
        request_model="base-model",
        expected_response_model="base-model",
        engine_provenance_ref="engine-provenance",
        engine_license_ref="engine-license",
        model_provenance_ref="base-provenance",
        model_license_ref="base-license",
        model_sha256=base_sha256,
    )
    challenger = ModelCandidate(
        candidate_id=spec.candidate_artifact_ref,
        provider_id=descriptor.provider_id,
        provider_kind=ProviderKind.LOCAL,
        request_model=descriptor.model_id,
        expected_response_model=descriptor.model_id,
        engine_provenance_ref="engine-provenance",
        engine_license_ref="engine-license",
        model_provenance_ref=descriptor.source_reference,
        model_license_ref=descriptor.license_reference,
        model_sha256=candidate_sha256,
    )
    return {
        "candidate_path": candidate_path,
        "challenger": challenger,
        "champion": champion,
        "descriptor": descriptor,
        "evaluation_set": evaluation_set,
        "evidence": evidence,
        "package": package,
        "spec": spec,
    }


def _bind(values: dict[str, object], *, allowed_root: Path):
    return bind_training_result_for_evaluation(
        spec=values["spec"],  # type: ignore[arg-type]
        evidence=values["evidence"],  # type: ignore[arg-type]
        package=values["package"],  # type: ignore[arg-type]
        candidate_path=values["candidate_path"],  # type: ignore[arg-type]
        descriptor=values["descriptor"],  # type: ignore[arg-type]
        champion=values["champion"],  # type: ignore[arg-type]
        challenger=values["challenger"],  # type: ignore[arg-type]
        evaluation_set=values["evaluation_set"],  # type: ignore[arg-type]
        allowed_root=allowed_root,
    )


def test_completed_training_binds_exact_old_new_and_held_out_identity(
    tmp_path: Path,
) -> None:
    values = _fixture(tmp_path)

    binding = _bind(values, allowed_root=tmp_path)

    evidence = values["evidence"]
    descriptor = values["descriptor"]
    evaluation_set = values["evaluation_set"]
    assert isinstance(evidence, TrainingRunEvidence)
    assert isinstance(descriptor, ModelArtifactDescriptor)
    assert isinstance(evaluation_set, EvaluationSet)
    assert binding.job_id == "job-1"
    assert binding.challenger_sha256 == evidence.candidate_sha256
    assert binding.descriptor_digest == descriptor.descriptor_digest
    assert binding.evaluation_set_sha256 == evaluation_set.content_sha256
    assert len(binding.binding_sha256) == 64
    assert binding.binding_sha256 == binding.binding_sha256


def test_incomplete_training_cannot_enter_evaluation_binding(tmp_path: Path) -> None:
    values = _fixture(tmp_path)
    evidence = values["evidence"]
    assert isinstance(evidence, TrainingRunEvidence)
    values["evidence"] = replace(
        evidence,
        state=TrainingRunState.PAUSED,
        candidate_sha256=None,
    )

    with pytest.raises(TrainingEvaluationBindingError, match="only completed"):
        _bind(values, allowed_root=tmp_path)


def test_training_digest_cannot_be_rebound_to_different_descriptor(tmp_path: Path) -> None:
    values = _fixture(tmp_path)
    descriptor = values["descriptor"]
    assert isinstance(descriptor, ModelArtifactDescriptor)
    values["descriptor"] = replace(descriptor, sha256=_sha(b"other-weights"))

    with pytest.raises(
        TrainingEvaluationBindingError,
        match="descriptor does not match completed training bytes",
    ):
        _bind(values, allowed_root=tmp_path)


def test_frozen_held_out_identity_cannot_be_substituted(tmp_path: Path) -> None:
    values = _fixture(tmp_path)
    values["evaluation_set"] = _evaluation_set(expected_text="four")

    with pytest.raises(
        TrainingEvaluationBindingError,
        match="evaluation set does not match",
    ):
        _bind(values, allowed_root=tmp_path)


def test_base_candidate_must_bind_training_base_digest(tmp_path: Path) -> None:
    values = _fixture(tmp_path)
    champion = values["champion"]
    assert isinstance(champion, ModelCandidate)
    values["champion"] = replace(champion, model_sha256=_sha(b"other-base"))

    with pytest.raises(
        TrainingEvaluationBindingError,
        match="base Model-Lab candidate digest",
    ):
        _bind(values, allowed_root=tmp_path)


def test_challenger_route_must_bind_descriptor_provider_and_model(
    tmp_path: Path,
) -> None:
    values = _fixture(tmp_path)
    challenger = values["challenger"]
    assert isinstance(challenger, ModelCandidate)
    values["challenger"] = replace(challenger, request_model="different-model")

    with pytest.raises(
        TrainingEvaluationBindingError,
        match="provider/model route",
    ):
        _bind(values, allowed_root=tmp_path)


def test_challenger_provenance_must_bind_descriptor(tmp_path: Path) -> None:
    values = _fixture(tmp_path)
    challenger = values["challenger"]
    assert isinstance(challenger, ModelCandidate)
    values["challenger"] = replace(
        challenger,
        model_provenance_ref="different-provenance",
    )

    with pytest.raises(
        TrainingEvaluationBindingError,
        match="challenger provenance",
    ):
        _bind(values, allowed_root=tmp_path)


def test_physical_candidate_tamper_fails_closed(tmp_path: Path) -> None:
    values = _fixture(tmp_path)
    candidate_path = values["candidate_path"]
    assert isinstance(candidate_path, Path)
    original = candidate_path.read_bytes()
    candidate_path.write_bytes(b"x" * len(original))

    with pytest.raises(
        TrainingEvaluationBindingError,
        match="physical artifact verification failed",
    ):
        _bind(values, allowed_root=tmp_path)


def test_candidate_must_remain_inside_allowed_root(tmp_path: Path) -> None:
    values = _fixture(tmp_path)
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()

    with pytest.raises(
        TrainingEvaluationBindingError,
        match="physical artifact verification failed",
    ):
        _bind(values, allowed_root=allowed_root)


def test_forged_completed_next_step_is_rejected(tmp_path: Path) -> None:
    values = _fixture(tmp_path)
    evidence = values["evidence"]
    assert isinstance(evidence, TrainingRunEvidence)
    values["evidence"] = replace(evidence, next_step=True)

    with pytest.raises(
        TrainingEvaluationBindingError,
        match="invalid next-step",
    ):
        _bind(values, allowed_root=tmp_path)


def test_mutated_frozen_package_is_revalidated_before_binding(tmp_path: Path) -> None:
    values = _fixture(tmp_path)
    package = values["package"]
    assert isinstance(package, FrozenLearningPackage)
    object.__setattr__(package, "base_artifact_sha256", _sha(b"forged-base"))

    with pytest.raises(
        TrainingEvaluationBindingError,
        match="frozen learning package is not canonical",
    ):
        _bind(values, allowed_root=tmp_path)


def test_cloud_descriptor_cannot_authorize_physical_trained_challenger(
    tmp_path: Path,
) -> None:
    values = _fixture(tmp_path)
    descriptor = values["descriptor"]
    assert isinstance(descriptor, ModelArtifactDescriptor)
    values["descriptor"] = replace(descriptor, kind=ModelArtifactKind.CLOUD)

    with pytest.raises(
        TrainingEvaluationBindingError,
        match="local model artifact",
    ):
        _bind(values, allowed_root=tmp_path)
