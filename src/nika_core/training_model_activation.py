from __future__ import annotations

from nika_core.experiments import ExperimentStatus
from nika_core.training_evaluation_comparison import AttestedTrainingComparisonResult
from nika_core.v01_model_settings import (
    ModelPromotionReceipt,
    ModelSetupError,
    V01ModelSettings,
)


class TrainingModelActivationError(RuntimeError):
    """Safe failure while applying or rolling back an attested Loop-C promotion."""


def _promotion_authority(
    result: AttestedTrainingComparisonResult,
) -> AttestedTrainingComparisonResult:
    if type(result) is not AttestedTrainingComparisonResult:
        raise TypeError(
            "result must be an exact AttestedTrainingComparisonResult"
        )
    try:
        canonical = result.revalidated()
    except (AttributeError, TypeError, ValueError) as exc:
        raise TrainingModelActivationError(
            "attested comparison evidence is not canonical"
        ) from exc

    snapshot = canonical.experiment_snapshot
    training = canonical.challenger_benchmark.binding.revalidated()
    if snapshot.status is not ExperimentStatus.PROMOTED:
        raise TrainingModelActivationError(
            "model activation requires a PROMOTED experiment decision"
        )
    if (
        snapshot.previous_champion_id != training.base_candidate_id
        or snapshot.selected_candidate_id != training.challenger_candidate_id
    ):
        raise TrainingModelActivationError(
            "promotion decision does not match the training binding"
        )
    return canonical


def activate_attested_training_promotion(
    *,
    result: AttestedTrainingComparisonResult,
    settings: V01ModelSettings,
    expected_revision: int,
) -> ModelPromotionReceipt:
    """Switch the durable default local model using exact attested promotion evidence.

    Existing task model bindings are not rewritten. Only future tasks observe the
    promoted default route through the canonical V01ModelSettings authority.
    """

    if type(settings) is not V01ModelSettings:
        raise TypeError("settings must be an exact V01ModelSettings")
    canonical = _promotion_authority(result)
    training = canonical.challenger_benchmark.binding.revalidated()
    try:
        return settings.activate_promoted_local_model(
            expected_revision=expected_revision,
            base_provider_id=training.base_provider_id,
            base_model_id=training.base_model_id,
            challenger_provider_id=training.challenger_provider_id,
            challenger_model_id=training.challenger_model_id,
            decision_sha256=canonical.evidence_sha256,
            binding_sha256=training.binding_sha256,
            base_artifact_sha256=training.base_sha256,
            base_descriptor_digest=training.base_descriptor_digest,
            challenger_artifact_sha256=training.challenger_sha256,
            challenger_descriptor_digest=training.descriptor_digest,
        )
    except ModelSetupError as exc:
        raise TrainingModelActivationError(
            "attested model promotion was rejected by the active route authority"
        ) from exc


def rollback_attested_training_promotion(
    *,
    result: AttestedTrainingComparisonResult,
    settings: V01ModelSettings,
    expected_revision: int,
) -> ModelPromotionReceipt:
    """Restore the exact pre-promotion route while the promotion still owns it."""

    if type(settings) is not V01ModelSettings:
        raise TypeError("settings must be an exact V01ModelSettings")
    canonical = _promotion_authority(result)
    training = canonical.challenger_benchmark.binding.revalidated()
    try:
        return settings.rollback_promoted_local_model(
            decision_sha256=canonical.evidence_sha256,
            binding_sha256=training.binding_sha256,
            base_artifact_sha256=training.base_sha256,
            base_descriptor_digest=training.base_descriptor_digest,
            challenger_artifact_sha256=training.challenger_sha256,
            challenger_descriptor_digest=training.descriptor_digest,
            expected_revision=expected_revision,
        )
    except ModelSetupError as exc:
        raise TrainingModelActivationError(
            "attested model promotion rollback was rejected by the active route authority"
        ) from exc


__all__ = [
    "TrainingModelActivationError",
    "activate_attested_training_promotion",
    "rollback_attested_training_promotion",
]
