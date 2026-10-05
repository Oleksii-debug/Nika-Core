from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nika_core.learning_package import LearningDataSplit
from nika_core.training_materials import (
    TrainingMaterialEvidence,
    TrainingMaterialSetEvidence,
)
from nika_core.training_runtime.contracts import (
    ArtifactIdentity,
    TrainingJobSpec,
    TrainingRunEvidence,
    TrainingRunState,
)

if TYPE_CHECKING:
    from nika_core.training_evaluation_comparison import (
        AttestedTrainingComparisonResult,
    )

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
_MAX_SIGNED_64 = (1 << 63) - 1
_MAX_TIERS = 8


class TrainingScaleError(RuntimeError):
    """Scale-progression evidence is absent, stale, or inconsistent."""


def _require_sha256(value: object, *, name: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise TrainingScaleError(f"{name} must be an exact lowercase SHA-256 digest")
    return value


def _require_token(value: object, *, name: str) -> str:
    if type(value) is not str or _TOKEN_RE.fullmatch(value) is None:
        raise TrainingScaleError(f"{name} must be a bounded canonical token")
    return value


def _require_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise TrainingScaleError(f"{name} must be non-empty canonical text")
    if any(not character.isprintable() for character in value):
        raise TrainingScaleError(f"{name} must not contain control characters")
    if len(value.encode("utf-8")) > 512:
        raise TrainingScaleError(f"{name} exceeds the configured byte limit")
    return value


def _require_positive_int(value: object, *, name: str) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_SIGNED_64:
        raise TrainingScaleError(f"{name} must be a positive signed-64 integer")
    return value


def _sha256_payload(payload: object, *, domain: bytes) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(domain + b"\x00" + encoded).hexdigest()


def _snapshot_artifact(value: ArtifactIdentity) -> ArtifactIdentity:
    if type(value) is not ArtifactIdentity:
        raise TrainingScaleError("base artifact must be an exact ArtifactIdentity")
    try:
        return ArtifactIdentity(
            artifact_ref=value.artifact_ref,
            sha256=value.sha256,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise TrainingScaleError("base artifact identity is not canonical") from exc


def _snapshot_material_evidence(
    value: TrainingMaterialSetEvidence,
) -> TrainingMaterialSetEvidence:
    if type(value) is not TrainingMaterialSetEvidence:
        raise TrainingScaleError(
            "training material evidence must be an exact TrainingMaterialSetEvidence"
        )
    try:
        materials = tuple(
            TrainingMaterialEvidence(
                split=item.split,
                artifact_sha256=item.artifact_sha256,
                provenance_sha256=item.provenance_sha256,
                license_evidence_sha256=item.license_evidence_sha256,
                record_count=item.record_count,
                byte_count=item.byte_count,
            )
            for item in value.materials
        )
        return TrainingMaterialSetEvidence(
            workspace_sha256=value.workspace_sha256,
            package_id=value.package_id,
            package_version=value.package_version,
            package_schema_version=value.package_schema_version,
            base_artifact_sha256=value.base_artifact_sha256,
            selection_policy_sha256=value.selection_policy_sha256,
            verification_sha256=value.verification_sha256,
            evaluation_set_sha256=value.evaluation_set_sha256,
            candidate_dataset_sha256=value.candidate_dataset_sha256,
            package_manifest_sha256=value.package_manifest_sha256,
            materials=materials,
            schema_version=value.schema_version,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise TrainingScaleError("training material evidence is not canonical") from exc


def _material_totals(
    evidence: TrainingMaterialSetEvidence,
) -> tuple[int, int, int, int]:
    training_records = 0
    training_bytes = 0
    validation_records = 0
    validation_bytes = 0
    for item in evidence.materials:
        if item.split is LearningDataSplit.TRAINING:
            training_records += item.record_count
            training_bytes += item.byte_count
        elif item.split is LearningDataSplit.VALIDATION:
            validation_records += item.record_count
            validation_bytes += item.byte_count
        else:  # pragma: no cover - material evidence reconstructs the frozen package
            raise TrainingScaleError("training material evidence contains an invalid split")
    return (
        training_records,
        training_bytes,
        validation_records,
        validation_bytes,
    )


@dataclass(frozen=True, slots=True)
class TrainingScaleTier:
    tier_id: str
    max_training_records: int
    max_training_bytes: int
    max_validation_records: int
    max_validation_bytes: int
    max_steps: int

    def __post_init__(self) -> None:
        _require_token(self.tier_id, name="tier_id")
        for value, name in (
            (self.max_training_records, "max_training_records"),
            (self.max_training_bytes, "max_training_bytes"),
            (self.max_validation_records, "max_validation_records"),
            (self.max_validation_bytes, "max_validation_bytes"),
            (self.max_steps, "max_steps"),
        ):
            _require_positive_int(value, name=name)


@dataclass(frozen=True, slots=True)
class TrainingScalePlan:
    plan_id: str
    evaluation_set_sha256: str
    tiers: tuple[TrainingScaleTier, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        _require_token(self.plan_id, name="plan_id")
        _require_sha256(self.evaluation_set_sha256, name="evaluation_set_sha256")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise TrainingScaleError("unsupported training scale plan schema")
        if type(self.tiers) is not tuple or not 1 <= len(self.tiers) <= _MAX_TIERS:
            raise TrainingScaleError("training scale plan tier count is outside the bound")
        if any(type(tier) is not TrainingScaleTier for tier in self.tiers):
            raise TrainingScaleError("training scale plan requires exact tier values")
        canonical = tuple(
            TrainingScaleTier(
                tier_id=tier.tier_id,
                max_training_records=tier.max_training_records,
                max_training_bytes=tier.max_training_bytes,
                max_validation_records=tier.max_validation_records,
                max_validation_bytes=tier.max_validation_bytes,
                max_steps=tier.max_steps,
            )
            for tier in self.tiers
        )
        object.__setattr__(self, "tiers", canonical)
        ids = tuple(tier.tier_id for tier in canonical)
        if len(ids) != len(set(ids)):
            raise TrainingScaleError("training scale tier IDs must be unique")
        for previous, current in zip(canonical, canonical[1:], strict=False):
            before = (
                previous.max_training_records,
                previous.max_training_bytes,
                previous.max_validation_records,
                previous.max_validation_bytes,
                previous.max_steps,
            )
            after = (
                current.max_training_records,
                current.max_training_bytes,
                current.max_validation_records,
                current.max_validation_bytes,
                current.max_steps,
            )
            if any(new < old for old, new in zip(before, after, strict=True)):
                raise TrainingScaleError("training scale limits must not decrease")
            if before == after:
                raise TrainingScaleError("each training scale tier must expand a bound")

    def revalidated(self) -> TrainingScalePlan:
        if type(self) is not TrainingScalePlan:
            raise TypeError("plan must be an exact TrainingScalePlan")
        try:
            return TrainingScalePlan(
                plan_id=self.plan_id,
                evaluation_set_sha256=self.evaluation_set_sha256,
                tiers=self.tiers,
                schema_version=self.schema_version,
            )
        except AttributeError as exc:
            raise TrainingScaleError("training scale plan fields are incomplete") from exc

    def canonical_payload(self) -> dict[str, object]:
        plan = self.revalidated()
        return {
            "evaluation_set_sha256": plan.evaluation_set_sha256,
            "plan_id": plan.plan_id,
            "schema_version": plan.schema_version,
            "tiers": [
                {
                    "max_steps": tier.max_steps,
                    "max_training_bytes": tier.max_training_bytes,
                    "max_training_records": tier.max_training_records,
                    "max_validation_bytes": tier.max_validation_bytes,
                    "max_validation_records": tier.max_validation_records,
                    "tier_id": tier.tier_id,
                }
                for tier in plan.tiers
            ],
        }

    @property
    def plan_sha256(self) -> str:
        return _sha256_payload(
            self.canonical_payload(),
            domain=b"nika-training-scale-plan-v1",
        )


@dataclass(frozen=True, slots=True, init=False)
class TrainingScaleProgressionProof:
    plan_sha256: str
    tier_index: int
    authorization_sha256: str
    job_id: str
    job_fingerprint: str
    base_artifact_ref: str
    base_sha256: str
    candidate_artifact_ref: str
    candidate_sha256: str
    frozen_package_sha256: str
    training_material_sha256: str
    execution_plan_sha256: str
    comparison_evidence_sha256: str
    evaluation_set_sha256: str

    def _validate(self) -> None:
        for value, name in (
            (self.plan_sha256, "plan_sha256"),
            (self.authorization_sha256, "authorization_sha256"),
            (self.job_fingerprint, "job_fingerprint"),
            (self.base_sha256, "base_sha256"),
            (self.candidate_sha256, "candidate_sha256"),
            (self.frozen_package_sha256, "frozen_package_sha256"),
            (self.training_material_sha256, "training_material_sha256"),
            (self.execution_plan_sha256, "execution_plan_sha256"),
            (self.comparison_evidence_sha256, "comparison_evidence_sha256"),
            (self.evaluation_set_sha256, "evaluation_set_sha256"),
        ):
            _require_sha256(value, name=name)
        if type(self.tier_index) is not int or self.tier_index < 0:
            raise TrainingScaleError("tier_index must be a non-negative integer")
        _require_text(self.job_id, name="job_id")
        _require_text(self.base_artifact_ref, name="base_artifact_ref")
        _require_text(self.candidate_artifact_ref, name="candidate_artifact_ref")
        if self.base_artifact_ref == self.candidate_artifact_ref:
            raise TrainingScaleError("progression proof cannot overwrite the base artifact")

    def revalidated(self) -> TrainingScaleProgressionProof:
        if type(self) is not TrainingScaleProgressionProof:
            raise TypeError("proof must be an exact TrainingScaleProgressionProof")
        try:
            self._validate()
            return _build_progression_proof(
                plan_sha256=self.plan_sha256,
                tier_index=self.tier_index,
                authorization_sha256=self.authorization_sha256,
                job_id=self.job_id,
                job_fingerprint=self.job_fingerprint,
                base_artifact_ref=self.base_artifact_ref,
                base_sha256=self.base_sha256,
                candidate_artifact_ref=self.candidate_artifact_ref,
                candidate_sha256=self.candidate_sha256,
                frozen_package_sha256=self.frozen_package_sha256,
                training_material_sha256=self.training_material_sha256,
                execution_plan_sha256=self.execution_plan_sha256,
                comparison_evidence_sha256=self.comparison_evidence_sha256,
                evaluation_set_sha256=self.evaluation_set_sha256,
            )
        except AttributeError as exc:
            raise TrainingScaleError("training progression proof fields are incomplete") from exc

    @property
    def proof_sha256(self) -> str:
        proof = self.revalidated()
        payload = {
            "authorization_sha256": proof.authorization_sha256,
            "base_artifact_ref": proof.base_artifact_ref,
            "base_sha256": proof.base_sha256,
            "candidate_artifact_ref": proof.candidate_artifact_ref,
            "candidate_sha256": proof.candidate_sha256,
            "comparison_evidence_sha256": proof.comparison_evidence_sha256,
            "evaluation_set_sha256": proof.evaluation_set_sha256,
            "execution_plan_sha256": proof.execution_plan_sha256,
            "frozen_package_sha256": proof.frozen_package_sha256,
            "job_fingerprint": proof.job_fingerprint,
            "job_id": proof.job_id,
            "plan_sha256": proof.plan_sha256,
            "tier_index": proof.tier_index,
            "training_material_sha256": proof.training_material_sha256,
        }
        return _sha256_payload(payload, domain=b"nika-training-scale-proof-v1")


def _build_progression_proof(
    *,
    plan_sha256: str,
    tier_index: int,
    authorization_sha256: str,
    job_id: str,
    job_fingerprint: str,
    base_artifact_ref: str,
    base_sha256: str,
    candidate_artifact_ref: str,
    candidate_sha256: str,
    frozen_package_sha256: str,
    training_material_sha256: str,
    execution_plan_sha256: str,
    comparison_evidence_sha256: str,
    evaluation_set_sha256: str,
) -> TrainingScaleProgressionProof:
    proof = object.__new__(TrainingScaleProgressionProof)
    for name, value in (
        ("plan_sha256", plan_sha256),
        ("tier_index", tier_index),
        ("authorization_sha256", authorization_sha256),
        ("job_id", job_id),
        ("job_fingerprint", job_fingerprint),
        ("base_artifact_ref", base_artifact_ref),
        ("base_sha256", base_sha256),
        ("candidate_artifact_ref", candidate_artifact_ref),
        ("candidate_sha256", candidate_sha256),
        ("frozen_package_sha256", frozen_package_sha256),
        ("training_material_sha256", training_material_sha256),
        ("execution_plan_sha256", execution_plan_sha256),
        ("comparison_evidence_sha256", comparison_evidence_sha256),
        ("evaluation_set_sha256", evaluation_set_sha256),
    ):
        object.__setattr__(proof, name, value)
    proof._validate()
    return proof


@dataclass(frozen=True, slots=True)
class TrainingScaleAuthorization:
    plan: TrainingScalePlan
    tier_index: int
    job_id: str
    base_artifact: ArtifactIdentity
    candidate_artifact_ref: str
    material_evidence: TrainingMaterialSetEvidence
    execution_plan_sha256: str
    max_steps: int
    resource_scope: str
    progression_proof: TrainingScaleProgressionProof | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise TrainingScaleError("unsupported training scale authorization schema")
        plan = self.plan.revalidated()
        object.__setattr__(self, "plan", plan)
        if type(self.tier_index) is not int or not 0 <= self.tier_index < len(plan.tiers):
            raise TrainingScaleError("training scale tier index is invalid")
        _require_text(self.job_id, name="job_id")
        base_artifact = _snapshot_artifact(self.base_artifact)
        object.__setattr__(self, "base_artifact", base_artifact)
        _require_text(self.candidate_artifact_ref, name="candidate_artifact_ref")
        if self.candidate_artifact_ref == base_artifact.artifact_ref:
            raise TrainingScaleError("candidate artifact must not overwrite the base artifact")
        evidence = _snapshot_material_evidence(self.material_evidence)
        object.__setattr__(self, "material_evidence", evidence)
        _require_sha256(self.execution_plan_sha256, name="execution_plan_sha256")
        _require_positive_int(self.max_steps, name="max_steps")
        _require_text(self.resource_scope, name="resource_scope")
        tier = plan.tiers[self.tier_index]
        if self.max_steps > tier.max_steps:
            raise TrainingScaleError("training max_steps exceeds the authorized scale tier")
        if evidence.base_artifact_sha256 != base_artifact.sha256:
            raise TrainingScaleError("scale authorization base artifact does not match package")
        if evidence.evaluation_set_sha256 != plan.evaluation_set_sha256:
            raise TrainingScaleError(
                "scale authorization evaluation set does not match the scale plan"
            )
        (
            training_records,
            training_bytes,
            validation_records,
            validation_bytes,
        ) = _material_totals(evidence)
        if (
            training_records > tier.max_training_records
            or training_bytes > tier.max_training_bytes
            or validation_records > tier.max_validation_records
            or validation_bytes > tier.max_validation_bytes
        ):
            raise TrainingScaleError("training package exceeds the authorized scale tier")

        if self.tier_index == 0:
            if self.progression_proof is not None:
                raise TrainingScaleError("pilot scale must not carry progression evidence")
        else:
            if type(self.progression_proof) is not TrainingScaleProgressionProof:
                raise TrainingScaleError(
                    "higher training scale requires exact progression evidence"
                )
            proof = self.progression_proof.revalidated()
            object.__setattr__(self, "progression_proof", proof)
            if proof.plan_sha256 != plan.plan_sha256:
                raise TrainingScaleError("progression proof belongs to another scale plan")
            if proof.tier_index != self.tier_index - 1:
                raise TrainingScaleError("training scale tiers cannot be skipped")
            if proof.evaluation_set_sha256 != plan.evaluation_set_sha256:
                raise TrainingScaleError("progression proof uses a different held-out set")
            if (
                base_artifact.artifact_ref != proof.candidate_artifact_ref
                or base_artifact.sha256 != proof.candidate_sha256
            ):
                raise TrainingScaleError(
                    "higher training scale must continue from the promoted candidate"
                )

    def revalidated(self) -> TrainingScaleAuthorization:
        if type(self) is not TrainingScaleAuthorization:
            raise TypeError("authorization must be an exact TrainingScaleAuthorization")
        try:
            return TrainingScaleAuthorization(
                plan=self.plan,
                tier_index=self.tier_index,
                job_id=self.job_id,
                base_artifact=self.base_artifact,
                candidate_artifact_ref=self.candidate_artifact_ref,
                material_evidence=self.material_evidence,
                execution_plan_sha256=self.execution_plan_sha256,
                max_steps=self.max_steps,
                resource_scope=self.resource_scope,
                progression_proof=self.progression_proof,
                schema_version=self.schema_version,
            )
        except AttributeError as exc:
            raise TrainingScaleError(
                "training scale authorization fields are incomplete"
            ) from exc

    def _payload_unchecked(self) -> dict[str, object]:
        proof = self.progression_proof
        return {
            "base_artifact_ref": self.base_artifact.artifact_ref,
            "base_sha256": self.base_artifact.sha256,
            "candidate_artifact_ref": self.candidate_artifact_ref,
            "candidate_dataset_sha256": self.material_evidence.candidate_dataset_sha256,
            "evaluation_set_sha256": self.material_evidence.evaluation_set_sha256,
            "execution_plan_sha256": self.execution_plan_sha256,
            "frozen_package_sha256": self.material_evidence.package_manifest_sha256,
            "job_id": self.job_id,
            "max_steps": self.max_steps,
            "plan_sha256": self.plan.plan_sha256,
            "progression_proof_sha256": None if proof is None else proof.proof_sha256,
            "resource_scope": self.resource_scope,
            "schema_version": self.schema_version,
            "tier_index": self.tier_index,
            "tier_id": self.plan.tiers[self.tier_index].tier_id,
            "training_material_sha256": self.material_evidence.training_material_sha256,
        }

    @property
    def authorization_sha256(self) -> str:
        authorization = self.revalidated()
        return _sha256_payload(
            authorization._payload_unchecked(),
            domain=b"nika-training-scale-authorization-v1",
        )

    def verify_for_runtime(
        self,
        *,
        spec: TrainingJobSpec,
        material_evidence: TrainingMaterialSetEvidence,
        execution_plan_sha256: str,
    ) -> TrainingScaleAuthorization:
        authorization = self.revalidated()
        if type(spec) is not TrainingJobSpec:
            raise TypeError("spec must be an exact TrainingJobSpec")
        current_materials = _snapshot_material_evidence(material_evidence)
        observed_plan = _require_sha256(
            execution_plan_sha256,
            name="execution_plan_sha256",
        )
        try:
            canonical_spec = TrainingJobSpec(
                job_id=spec.job_id,
                task_id=spec.task_id,
                project_id=spec.project_id,
                owner_id=spec.owner_id,
                base_artifact=_snapshot_artifact(spec.base_artifact),
                frozen_package_sha256=spec.frozen_package_sha256,
                training_material_sha256=spec.training_material_sha256,
                scale_authorization_sha256=spec.scale_authorization_sha256,
                candidate_artifact_ref=spec.candidate_artifact_ref,
                max_steps=spec.max_steps,
                resource_scope=spec.resource_scope,
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise TrainingScaleError("training job specification is not canonical") from exc

        if (
            canonical_spec.scale_authorization_sha256
            != authorization.authorization_sha256
            or canonical_spec.job_id != authorization.job_id
            or canonical_spec.base_artifact != authorization.base_artifact
            or canonical_spec.candidate_artifact_ref
            != authorization.candidate_artifact_ref
            or canonical_spec.frozen_package_sha256
            != authorization.material_evidence.package_manifest_sha256
            or canonical_spec.training_material_sha256
            != authorization.material_evidence.training_material_sha256
            or canonical_spec.max_steps != authorization.max_steps
            or canonical_spec.resource_scope != authorization.resource_scope
            or observed_plan != authorization.execution_plan_sha256
            or current_materials.canonical_payload()
            != authorization.material_evidence.canonical_payload()
        ):
            raise TrainingScaleError(
                "training scale authorization does not match the exact training job"
            )
        return authorization


def authorize_training_scale(
    *,
    plan: TrainingScalePlan,
    tier_id: str,
    job_id: str,
    base_artifact: ArtifactIdentity,
    candidate_artifact_ref: str,
    material_evidence: TrainingMaterialSetEvidence,
    execution_plan_sha256: str,
    max_steps: int,
    resource_scope: str = "model_training",
    progression_proof: TrainingScaleProgressionProof | None = None,
) -> TrainingScaleAuthorization:
    """Issue one bounded run authorization from frozen, self-verifying evidence."""

    canonical_plan = plan.revalidated()
    wanted_tier = _require_token(tier_id, name="tier_id")
    matching = tuple(
        index
        for index, tier in enumerate(canonical_plan.tiers)
        if tier.tier_id == wanted_tier
    )
    if len(matching) != 1:
        raise TrainingScaleError("requested training scale tier is not declared")
    return TrainingScaleAuthorization(
        plan=canonical_plan,
        tier_index=matching[0],
        job_id=job_id,
        base_artifact=base_artifact,
        candidate_artifact_ref=candidate_artifact_ref,
        material_evidence=material_evidence,
        execution_plan_sha256=execution_plan_sha256,
        max_steps=max_steps,
        resource_scope=resource_scope,
        progression_proof=progression_proof,
    ).revalidated()


def build_scale_progression_proof(
    *,
    plan: TrainingScalePlan,
    authorization: TrainingScaleAuthorization,
    run: TrainingRunEvidence,
    comparison: AttestedTrainingComparisonResult,
) -> TrainingScaleProgressionProof:
    """Convert one completed and promoted Loop-C cycle into next-tier authority."""

    canonical_plan = plan.revalidated()
    canonical_authorization = authorization.revalidated()
    if canonical_authorization.plan.plan_sha256 != canonical_plan.plan_sha256:
        raise TrainingScaleError("authorization belongs to another scale plan")
    if type(run) is not TrainingRunEvidence:
        raise TypeError("run must be an exact TrainingRunEvidence")
    try:
        base = _snapshot_artifact(run.base_artifact)
        job_id = _require_text(run.job_id, name="training run job_id")
        job_fingerprint = _require_sha256(
            run.job_fingerprint,
            name="training run job_fingerprint",
        )
        frozen_package_sha256 = _require_sha256(
            run.frozen_package_sha256,
            name="training run frozen_package_sha256",
        )
        training_material_sha256 = _require_sha256(
            run.training_material_sha256,
            name="training run training_material_sha256",
        )
        scale_authorization_sha256 = _require_sha256(
            run.scale_authorization_sha256,
            name="training run scale_authorization_sha256",
        )
        execution_plan_sha256 = _require_sha256(
            run.execution_plan_sha256,
            name="training run execution_plan_sha256",
        )
        candidate_artifact_ref = _require_text(
            run.candidate_artifact_ref,
            name="training run candidate_artifact_ref",
        )
        candidate_sha256 = _require_sha256(
            run.candidate_sha256,
            name="training run candidate_sha256",
        )
    except AttributeError as exc:
        raise TrainingScaleError("training run evidence is incomplete") from exc
    if run.state is not TrainingRunState.COMPLETED:
        raise TrainingScaleError("scale progression requires completed training")
    if type(run.next_step) is not int or not 1 <= run.next_step <= authorization.max_steps:
        raise TrainingScaleError("completed training carries an invalid step boundary")
    if (
        job_id != canonical_authorization.job_id
        or base != canonical_authorization.base_artifact
        or candidate_artifact_ref != canonical_authorization.candidate_artifact_ref
        or frozen_package_sha256
        != canonical_authorization.material_evidence.package_manifest_sha256
        or training_material_sha256
        != canonical_authorization.material_evidence.training_material_sha256
        or scale_authorization_sha256
        != canonical_authorization.authorization_sha256
        or execution_plan_sha256 != canonical_authorization.execution_plan_sha256
    ):
        raise TrainingScaleError(
            "completed run does not match its training scale authorization"
        )

    from nika_core.experiments import ExperimentStatus
    from nika_core.training_evaluation_comparison import (
        AttestedTrainingComparisonResult,
    )

    if type(comparison) is not AttestedTrainingComparisonResult:
        raise TypeError(
            "comparison must be an exact AttestedTrainingComparisonResult"
        )
    try:
        canonical_comparison = comparison.revalidated()
        binding = canonical_comparison.challenger_benchmark.binding.revalidated()
    except (AttributeError, TypeError, ValueError) as exc:
        raise TrainingScaleError("attested old/new comparison is not canonical") from exc
    snapshot = canonical_comparison.experiment_snapshot
    if snapshot.status is not ExperimentStatus.PROMOTED:
        raise TrainingScaleError("scale progression requires a PROMOTED comparison")
    if snapshot.selected_candidate_id != binding.challenger_candidate_id:
        raise TrainingScaleError("promotion decision does not select the trained challenger")
    if (
        binding.job_id != job_id
        or binding.base_sha256 != base.sha256
        or binding.challenger_sha256 != candidate_sha256
        or binding.candidate_artifact_ref != candidate_artifact_ref
        or binding.frozen_package_sha256 != frozen_package_sha256
        or binding.execution_plan_sha256 != execution_plan_sha256
        or binding.evaluation_set_sha256 != canonical_plan.evaluation_set_sha256
    ):
        raise TrainingScaleError(
            "promotion evidence does not match the completed authorized run"
        )

    return _build_progression_proof(
        plan_sha256=canonical_plan.plan_sha256,
        tier_index=canonical_authorization.tier_index,
        authorization_sha256=canonical_authorization.authorization_sha256,
        job_id=job_id,
        job_fingerprint=job_fingerprint,
        base_artifact_ref=base.artifact_ref,
        base_sha256=base.sha256,
        candidate_artifact_ref=candidate_artifact_ref,
        candidate_sha256=candidate_sha256,
        frozen_package_sha256=frozen_package_sha256,
        training_material_sha256=training_material_sha256,
        execution_plan_sha256=execution_plan_sha256,
        comparison_evidence_sha256=canonical_comparison.evidence_sha256,
        evaluation_set_sha256=canonical_plan.evaluation_set_sha256,
    )
