from __future__ import annotations

import hashlib

import pytest

import nika_core.training_scale as scale
from nika_core.learning_package import FrozenLearningPackage, LearningDataSplit, LearningShard
from nika_core.training_materials import (
    TrainingMaterialEvidence,
    TrainingMaterialSetEvidence,
)
from nika_core.training_scale import (
    TrainingScaleError,
    TrainingScalePlan,
    TrainingScaleProgressionProof,
    TrainingScaleTier,
    authorize_training_scale,
)
from nika_core.training_runtime import ArtifactIdentity, TrainingJobSpec


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _material_evidence(
    *,
    base_sha256: str | None = None,
    training_records: int = 1,
) -> TrainingMaterialSetEvidence:
    base_sha256 = base_sha256 or _sha(b"base")
    training_body = b"training-records"
    validation_body = b"validation-record"
    shards = (
        LearningShard(
            split=LearningDataSplit.TRAINING,
            artifact_sha256=_sha(training_body),
            provenance_sha256=_sha(b"training-provenance"),
            license_evidence_sha256=_sha(b"training-license"),
            record_count=training_records,
            byte_count=len(training_body),
        ),
        LearningShard(
            split=LearningDataSplit.VALIDATION,
            artifact_sha256=_sha(validation_body),
            provenance_sha256=_sha(b"validation-provenance"),
            license_evidence_sha256=_sha(b"validation-license"),
            record_count=1,
            byte_count=len(validation_body),
        ),
    )
    package = FrozenLearningPackage.freeze(
        package_id="scale-package",
        package_version=str(training_records),
        base_artifact_sha256=base_sha256,
        selection_policy_sha256=_sha(b"selection"),
        verification_sha256=_sha(b"verification"),
        evaluation_set_sha256=_sha(b"held-out"),
        shards=shards,
    )
    return TrainingMaterialSetEvidence.from_package(
        package,
        workspace_sha256=_sha(b"workspace"),
        materials=tuple(TrainingMaterialEvidence.from_shard(item) for item in shards),
    )


def _plan(evidence: TrainingMaterialSetEvidence) -> TrainingScalePlan:
    training = next(
        item for item in evidence.materials if item.split is LearningDataSplit.TRAINING
    )
    validation = next(
        item for item in evidence.materials if item.split is LearningDataSplit.VALIDATION
    )
    return TrainingScalePlan(
        plan_id="scale-test",
        evaluation_set_sha256=evidence.evaluation_set_sha256,
        tiers=(
            TrainingScaleTier(
                tier_id="pilot",
                max_training_records=training.record_count,
                max_training_bytes=training.byte_count,
                max_validation_records=validation.record_count,
                max_validation_bytes=validation.byte_count,
                max_steps=2,
            ),
            TrainingScaleTier(
                tier_id="small",
                max_training_records=training.record_count + 10,
                max_training_bytes=training.byte_count + 1024,
                max_validation_records=validation.record_count + 10,
                max_validation_bytes=validation.byte_count + 1024,
                max_steps=8,
            ),
        ),
    )


def _pilot_authorization(evidence: TrainingMaterialSetEvidence):
    return authorize_training_scale(
        plan=_plan(evidence),
        tier_id="pilot",
        job_id="pilot-job",
        base_artifact=ArtifactIdentity("models/base", evidence.base_artifact_sha256),
        candidate_artifact_ref="models/pilot-candidate",
        material_evidence=evidence,
        execution_plan_sha256=_sha(b"pilot-plan"),
        max_steps=2,
    )


def test_scale_plan_canonical_payload_round_trip_preserves_authority() -> None:
    evidence = _material_evidence()
    plan = _plan(evidence)

    restored = TrainingScalePlan.from_canonical_payload(plan.canonical_payload())

    assert restored == plan
    assert restored.canonical_payload() == plan.canonical_payload()
    assert restored.plan_sha256 == plan.plan_sha256


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: value.update({"unexpected": "field"}),
        lambda value: value.pop("plan_id"),
        lambda value: value.update({"tiers": tuple(value["tiers"])}),
        lambda value: value["tiers"][0].update({"unexpected": "field"}),
        lambda value: value["tiers"][0].update({"max_steps": True}),
    ),
)
def test_scale_plan_rehydration_rejects_noncanonical_payload(mutate: object) -> None:
    payload = _plan(_material_evidence()).canonical_payload()
    mutate(payload)  # type: ignore[operator]

    with pytest.raises(TrainingScaleError):
        TrainingScalePlan.from_canonical_payload(payload)


def test_scale_plan_rejects_decreasing_limits() -> None:
    evidence = _material_evidence()
    training = next(
        item for item in evidence.materials if item.split is LearningDataSplit.TRAINING
    )
    validation = next(
        item for item in evidence.materials if item.split is LearningDataSplit.VALIDATION
    )

    with pytest.raises(TrainingScaleError, match="must not decrease"):
        TrainingScalePlan(
            plan_id="bad-scale",
            evaluation_set_sha256=evidence.evaluation_set_sha256,
            tiers=(
                TrainingScaleTier(
                    tier_id="pilot",
                    max_training_records=2,
                    max_training_bytes=training.byte_count,
                    max_validation_records=validation.record_count,
                    max_validation_bytes=validation.byte_count,
                    max_steps=2,
                ),
                TrainingScaleTier(
                    tier_id="small",
                    max_training_records=1,
                    max_training_bytes=training.byte_count + 1,
                    max_validation_records=validation.record_count,
                    max_validation_bytes=validation.byte_count,
                    max_steps=3,
                ),
            ),
        )


def test_pilot_authorization_is_deterministic_and_material_bound() -> None:
    evidence = _material_evidence()
    first = _pilot_authorization(evidence)
    second = _pilot_authorization(evidence)

    assert first.authorization_sha256 == second.authorization_sha256
    assert first.material_evidence.training_material_sha256 == evidence.training_material_sha256
    assert first.plan.tiers[first.tier_index].tier_id == "pilot"
    assert first.progression_proof is None


def test_scale_authorization_rejects_package_above_tier_bound() -> None:
    evidence = _material_evidence(training_records=2)
    validation = next(
        item for item in evidence.materials if item.split is LearningDataSplit.VALIDATION
    )
    plan = TrainingScalePlan(
        plan_id="undersized-plan",
        evaluation_set_sha256=evidence.evaluation_set_sha256,
        tiers=(
            TrainingScaleTier(
                tier_id="pilot",
                max_training_records=1,
                max_training_bytes=1,
                max_validation_records=validation.record_count,
                max_validation_bytes=validation.byte_count,
                max_steps=1,
            ),
        ),
    )

    with pytest.raises(TrainingScaleError, match="exceeds"):
        authorize_training_scale(
            plan=plan,
            tier_id="pilot",
            job_id="oversized",
            base_artifact=ArtifactIdentity("models/base", evidence.base_artifact_sha256),
            candidate_artifact_ref="models/candidate",
            material_evidence=evidence,
            execution_plan_sha256=_sha(b"plan"),
            max_steps=1,
        )


def _progression_payload(plan: TrainingScalePlan) -> dict[str, object]:
    return {
        "authorization_sha256": _sha(b"authorization"),
        "base_artifact_ref": "models/base",
        "base_sha256": _sha(b"base"),
        "candidate_artifact_ref": "models/pilot-candidate",
        "candidate_sha256": _sha(b"candidate"),
        "comparison_evidence_sha256": _sha(b"comparison"),
        "evaluation_set_sha256": plan.evaluation_set_sha256,
        "execution_plan_sha256": _sha(b"execution-plan"),
        "frozen_package_sha256": _sha(b"frozen-package"),
        "job_fingerprint": _sha(b"job-fingerprint"),
        "job_id": "pilot-job",
        "plan_sha256": plan.plan_sha256,
        "tier_index": 0,
        "training_material_sha256": _sha(b"training-material"),
    }


def _trusted_progression_proof(
    plan: TrainingScalePlan,
) -> TrainingScaleProgressionProof:
    payload = _progression_payload(plan)
    return scale._build_progression_proof(
        plan_sha256=payload["plan_sha256"],
        tier_index=payload["tier_index"],
        authorization_sha256=payload["authorization_sha256"],
        job_id=payload["job_id"],
        job_fingerprint=payload["job_fingerprint"],
        base_artifact_ref=payload["base_artifact_ref"],
        base_sha256=payload["base_sha256"],
        candidate_artifact_ref=payload["candidate_artifact_ref"],
        candidate_sha256=payload["candidate_sha256"],
        frozen_package_sha256=payload["frozen_package_sha256"],
        training_material_sha256=payload["training_material_sha256"],
        execution_plan_sha256=payload["execution_plan_sha256"],
        comparison_evidence_sha256=payload["comparison_evidence_sha256"],
        evaluation_set_sha256=payload["evaluation_set_sha256"],
    )


def test_progression_payload_cannot_restore_authority_without_trusted_inputs() -> None:
    plan = _plan(_material_evidence())

    with pytest.raises(TypeError):
        TrainingScaleProgressionProof.from_canonical_payload(
            _progression_payload(plan)
        )


def test_progression_serialized_claim_validation_is_shape_only() -> None:
    plan = _plan(_material_evidence())
    payload = _progression_payload(plan)

    validated = TrainingScaleProgressionProof.validate_serialized_claim(payload)

    assert validated == payload
    assert validated is not payload
    with pytest.raises(TypeError):
        TrainingScaleProgressionProof.from_canonical_payload(validated)


def test_progression_restoration_returns_only_independently_rebuilt_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(_material_evidence())
    trusted = _trusted_progression_proof(plan)
    authorization = object()
    run = object()
    comparison = object()

    def rebuild(**kwargs: object) -> TrainingScaleProgressionProof:
        assert kwargs == {
            "plan": plan,
            "authorization": authorization,
            "run": run,
            "comparison": comparison,
        }
        return trusted

    monkeypatch.setattr(scale, "build_scale_progression_proof", rebuild)

    restored = TrainingScaleProgressionProof.from_canonical_payload(
        trusted.canonical_payload(),
        plan=plan,
        authorization=authorization,  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        comparison=comparison,  # type: ignore[arg-type]
    )

    assert restored is trusted
    assert restored.proof_sha256 == trusted.proof_sha256


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: value.update({"unexpected": "field"}),
        lambda value: value.pop("authorization_sha256"),
        lambda value: value.update({"tier_index": True}),
        lambda value: value.update({"candidate_sha256": "A" * 64}),
        lambda value: value.update({"job_id": " bad "}),
    ),
)
def test_progression_proof_rehydration_rejects_noncanonical_payload(
    mutate: object,
) -> None:
    plan = _plan(_material_evidence())
    payload = _progression_payload(plan)
    mutate(payload)  # type: ignore[operator]

    with pytest.raises(TrainingScaleError):
        TrainingScaleProgressionProof.from_canonical_payload(
            payload,
            plan=plan,
            authorization=object(),  # type: ignore[arg-type]
            run=object(),  # type: ignore[arg-type]
            comparison=object(),  # type: ignore[arg-type]
        )


def test_progression_restoration_rejects_payload_not_matching_trusted_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(_material_evidence())
    trusted = _trusted_progression_proof(plan)
    payload = trusted.canonical_payload()
    payload["comparison_evidence_sha256"] = "0" * 64

    monkeypatch.setattr(
        scale,
        "build_scale_progression_proof",
        lambda **_: trusted,
    )

    with pytest.raises(TrainingScaleError, match="trusted prior-run authority"):
        TrainingScaleProgressionProof.from_canonical_payload(
            payload,
            plan=plan,
            authorization=object(),  # type: ignore[arg-type]
            run=object(),  # type: ignore[arg-type]
            comparison=object(),  # type: ignore[arg-type]
        )


def test_higher_scale_requires_non_forgeable_progression_proof() -> None:
    evidence = _material_evidence()
    plan = _plan(evidence)

    with pytest.raises(TrainingScaleError, match="requires exact progression"):
        authorize_training_scale(
            plan=plan,
            tier_id="small",
            job_id="small-job",
            base_artifact=ArtifactIdentity("models/base", evidence.base_artifact_sha256),
            candidate_artifact_ref="models/small-candidate",
            material_evidence=evidence,
            execution_plan_sha256=_sha(b"small-plan"),
            max_steps=4,
        )

    with pytest.raises(TypeError):
        TrainingScaleProgressionProof(  # type: ignore[call-arg]
            plan_sha256=plan.plan_sha256,
        )


def test_runtime_verification_binds_job_spec_to_authorization() -> None:
    evidence = _material_evidence()
    authorization = _pilot_authorization(evidence)
    spec = TrainingJobSpec(
        job_id=authorization.job_id,
        task_id="pilot-task",
        project_id="project",
        owner_id="owner",
        base_artifact=authorization.base_artifact,
        frozen_package_sha256=evidence.package_manifest_sha256,
        training_material_sha256=evidence.training_material_sha256,
        scale_authorization_sha256=authorization.authorization_sha256,
        candidate_artifact_ref=authorization.candidate_artifact_ref,
        max_steps=authorization.max_steps,
    )

    verified = authorization.verify_for_runtime(
        spec=spec,
        material_evidence=evidence,
        execution_plan_sha256=authorization.execution_plan_sha256,
    )
    assert verified.authorization_sha256 == authorization.authorization_sha256

    wrong = TrainingJobSpec(
        job_id=spec.job_id,
        task_id=spec.task_id,
        project_id=spec.project_id,
        owner_id=spec.owner_id,
        base_artifact=spec.base_artifact,
        frozen_package_sha256=spec.frozen_package_sha256,
        training_material_sha256=spec.training_material_sha256,
        scale_authorization_sha256="0" * 64,
        candidate_artifact_ref=spec.candidate_artifact_ref,
        max_steps=spec.max_steps,
    )
    with pytest.raises(TrainingScaleError, match="exact training job"):
        authorization.verify_for_runtime(
            spec=wrong,
            material_evidence=evidence,
            execution_plan_sha256=authorization.execution_plan_sha256,
        )


def test_mutated_nested_material_evidence_is_revalidated() -> None:
    evidence = _material_evidence()
    authorization = _pilot_authorization(evidence)
    object.__setattr__(
        authorization.material_evidence,
        "candidate_dataset_sha256",
        "0" * 64,
    )

    with pytest.raises(TrainingScaleError, match="not canonical"):
        _ = authorization.authorization_sha256
