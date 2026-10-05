from __future__ import annotations

import hashlib
import json
import secrets

from nika_core.experiments import ExperimentStatus
from nika_core.model_gateway.contracts import (
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    PrivacyClass,
    ProviderKind,
)
from nika_core.training_evaluation_attestation import (
    AttestedModelCompletionResult,
    AttestedTrainingCandidateGateway,
    LoadedModelAttestedCompletionPort,
)
from nika_core.training_evaluation_comparison import AttestedTrainingComparisonResult
from nika_core.training_evaluation_binding import TrainingEvaluationBinding
from nika_core.training_ollama_manifest import (
    OllamaManifestAuthority,
    OllamaManifestAuthorityError,
    OllamaPreparedModelBinding,
    OllamaPromotionManifestStore,
    OllamaPromotionManifestStoreError,
)
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


def _canonical_sha256(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _activation_probe(
    *,
    canonical: AttestedTrainingComparisonResult,
    training: TrainingEvaluationBinding,
) -> ModelRequest:
    return ModelRequest(
        request_id=(
            "nika-model-activation-"
            f"{canonical.evidence_sha256[:16]}-{secrets.token_hex(16)}"
        ),
        messages=(
            ModelMessage(
                role="user",
                content="Return a non-empty response for model activation attestation.",
            ),
        ),
        model=training.challenger_model_id,
        provider_id=training.challenger_provider_id,
        provider_kind=ProviderKind.LOCAL,
        fallback_provider_ids=(),
        privacy=PrivacyClass.PUBLIC,
        timeout_seconds=30.0,
        temperature=0.0,
        metadata={
            "model_candidate_id": training.challenger_candidate_id,
            "evaluation_set_sha256": training.evaluation_set_sha256,
        },
    )


def _activation_request_sha256(request: ModelRequest) -> str:
    return _canonical_sha256(
        {
            "request_id": request.request_id,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in request.messages
            ],
            "model": request.model,
            "provider_id": request.provider_id,
            "provider_kind": (
                request.provider_kind.value
                if request.provider_kind is not None
                else None
            ),
            "fallback_provider_ids": list(request.fallback_provider_ids),
            "privacy": request.privacy.value,
            "timeout_seconds": float(request.timeout_seconds),
            "temperature": request.temperature,
            "metadata": dict(request.metadata),
        }
    )


def _activation_attestation_sha256(
    result: AttestedModelCompletionResult,
) -> str:
    if type(result) is not AttestedModelCompletionResult:
        raise TypeError(
            "activation result must be an exact AttestedModelCompletionResult"
        )
    attestation = result.attestation.revalidated()
    payload: dict[str, object] = {
        "request_id": attestation.request_id,
        "binding_sha256": attestation.binding_sha256,
        "provider_id": attestation.provider_id,
        "model_id": attestation.model_id,
        "artifact_sha256": attestation.artifact_sha256,
        "descriptor_digest": attestation.descriptor_digest,
        "attestor_id": attestation.attestor_id,
        "attestor_sha256": attestation.attestor_sha256,
    }
    if attestation.provider_manifest_sha256 is not None:
        payload["provider_manifest_sha256"] = attestation.provider_manifest_sha256
    return _canonical_sha256(payload)


def _apply_promotion(
    *,
    canonical: AttestedTrainingComparisonResult,
    settings: V01ModelSettings,
    expected_revision: int,
    activation_request_sha256: str,
    activation_attestation_sha256: str,
) -> ModelPromotionReceipt:
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
            activation_request_sha256=activation_request_sha256,
            activation_attestation_sha256=activation_attestation_sha256,
        )
    except ModelSetupError as exc:
        raise TrainingModelActivationError(
            "attested model promotion was rejected by the active route authority"
        ) from exc


async def _bind_promotion_manifests(
    *,
    canonical: AttestedTrainingComparisonResult,
    training: TrainingEvaluationBinding,
    manifest_store: OllamaPromotionManifestStore,
    manifest_authority: OllamaManifestAuthority,
    base_prepared_model: OllamaPreparedModelBinding,
    challenger_prepared_model: OllamaPreparedModelBinding,
) -> tuple[OllamaPreparedModelBinding, OllamaPreparedModelBinding]:
    if type(manifest_store) is not OllamaPromotionManifestStore:
        raise TypeError("manifest_store must be an exact OllamaPromotionManifestStore")
    if type(manifest_authority) is not OllamaManifestAuthority:
        raise TypeError("manifest_authority must be an exact OllamaManifestAuthority")
    try:
        base = base_prepared_model.revalidated()
        challenger = challenger_prepared_model.revalidated()
    except (AttributeError, TypeError, ValueError) as exc:
        raise TrainingModelActivationError(
            "prepared Ollama manifest evidence is not canonical"
        ) from exc
    if (
        base.route_model_id != training.base_model_id
        or base.artifact_sha256 != training.base_sha256
        or base.descriptor_digest != training.base_descriptor_digest
        or challenger.route_model_id != training.challenger_model_id
        or challenger.artifact_sha256 != training.challenger_sha256
        or challenger.descriptor_digest != training.descriptor_digest
    ):
        raise TrainingModelActivationError(
            "prepared Ollama manifest evidence does not match the training binding"
        )
    if (
        canonical.champion_provider_manifest_sha256 is None
        or canonical.challenger_provider_manifest_sha256 is None
    ):
        raise TrainingModelActivationError(
            "comparison lacks provider manifest evidence required for activation"
        )
    if (
        base.provider_manifest_sha256
        != canonical.champion_provider_manifest_sha256
        or challenger.provider_manifest_sha256
        != canonical.challenger_provider_manifest_sha256
    ):
        raise TrainingModelActivationError(
            "prepared Ollama manifests do not match evaluated provider manifests"
        )
    if (
        base.endpoint_sha256 != challenger.endpoint_sha256
        or base.endpoint_sha256 != manifest_authority.endpoint_sha256
    ):
        raise TrainingModelActivationError(
            "prepared Ollama models do not match the active endpoint authority"
        )
    try:
        await manifest_authority.assert_available(base)
        await manifest_authority.assert_available(challenger)
        manifest_store.save_pair(
            decision_sha256=canonical.evidence_sha256,
            binding_sha256=training.binding_sha256,
            base=base,
            challenger=challenger,
        )
    except (OllamaManifestAuthorityError, OllamaPromotionManifestStoreError) as exc:
        raise TrainingModelActivationError(
            "prepared Ollama provider manifest could not be revalidated"
        ) from exc
    return base, challenger


def _require_persisted_promotion_manifests(
    *,
    canonical: AttestedTrainingComparisonResult,
    training: TrainingEvaluationBinding,
    settings: V01ModelSettings,
    manifest_store: OllamaPromotionManifestStore | None,
) -> None:
    if manifest_store is None:
        raise TrainingModelActivationError(
            "durable Ollama provider manifest authority is required for promotion retry"
        )
    if type(manifest_store) is not OllamaPromotionManifestStore:
        raise TypeError("manifest_store must be an exact OllamaPromotionManifestStore")
    snapshot = settings.snapshot()
    base_url = snapshot.get("base_url")
    if type(base_url) is not str:
        raise TrainingModelActivationError(
            "active Ollama endpoint is unavailable for provider manifest recovery"
        )
    try:
        base = manifest_store.resolve(
            decision_sha256=canonical.evidence_sha256,
            binding_sha256=training.binding_sha256,
            role="rollback",
            artifact_sha256=training.base_sha256,
            descriptor_digest=training.base_descriptor_digest,
            route_model_id=training.base_model_id,
            base_url=base_url,
        )
        challenger = manifest_store.resolve(
            decision_sha256=canonical.evidence_sha256,
            binding_sha256=training.binding_sha256,
            role="challenger",
            artifact_sha256=training.challenger_sha256,
            descriptor_digest=training.descriptor_digest,
            route_model_id=training.challenger_model_id,
            base_url=base_url,
        )
    except OllamaPromotionManifestStoreError as exc:
        raise TrainingModelActivationError(
            "existing promotion lacks valid durable Ollama provider manifest authority"
        ) from exc
    if (
        canonical.champion_provider_manifest_sha256 is None
        or canonical.challenger_provider_manifest_sha256 is None
        or base.provider_manifest_sha256
        != canonical.champion_provider_manifest_sha256
        or challenger.provider_manifest_sha256
        != canonical.challenger_provider_manifest_sha256
    ):
        raise TrainingModelActivationError(
            "durable Ollama manifests do not match evaluated provider manifests"
        )


async def activate_attested_training_promotion(
    *,
    result: AttestedTrainingComparisonResult,
    settings: V01ModelSettings,
    expected_revision: int,
    effect_port: LoadedModelAttestedCompletionPort | None = None,
    manifest_store: OllamaPromotionManifestStore | None = None,
    manifest_authority: OllamaManifestAuthority | None = None,
    base_prepared_model: OllamaPreparedModelBinding | None = None,
    challenger_prepared_model: OllamaPreparedModelBinding | None = None,
) -> ModelPromotionReceipt:
    """Activate a promoted local model only after a fresh loaded-byte attestation.

    Existing task model bindings are not rewritten. A committed retry returns the
    durable receipt without creating a second provider effect. A first activation
    must freshly execute the same trusted attestor authority used by the comparison
    and prove that the challenger route still loads the exact evaluated artifact.
    """

    if type(settings) is not V01ModelSettings:
        raise TypeError("settings must be an exact V01ModelSettings")
    canonical = _promotion_authority(result)
    training = canonical.challenger_benchmark.binding.revalidated()

    try:
        existing = settings.promotion_receipt(canonical.evidence_sha256)
    except ModelSetupError as exc:
        raise TrainingModelActivationError(
            "durable model promotion evidence could not be read"
        ) from exc
    if existing is not None:
        if (
            existing.activation_request_sha256 is None
            or existing.activation_attestation_sha256 is None
        ):
            raise TrainingModelActivationError(
                "existing promotion predates fresh loaded-model attestation; "
                "rollback or reevaluate before activation"
            )
        _require_persisted_promotion_manifests(
            canonical=canonical,
            training=training,
            settings=settings,
            manifest_store=manifest_store,
        )
        return _apply_promotion(
            canonical=canonical,
            settings=settings,
            expected_revision=expected_revision,
            activation_request_sha256=existing.activation_request_sha256,
            activation_attestation_sha256=existing.activation_attestation_sha256,
        )

    try:
        snapshot = settings.snapshot()
    except ModelSetupError as exc:
        raise TrainingModelActivationError(
            "active model route could not be revalidated before activation"
        ) from exc
    if (
        snapshot.get("revision") != expected_revision
        or snapshot.get("route_kind") != "ollama"
        or snapshot.get("provider_id") != training.base_provider_id
        or snapshot.get("model") != training.base_model_id
    ):
        raise TrainingModelActivationError(
            "current model route no longer matches the evaluated champion"
        )
    if (
        training.base_provider_id != "ollama"
        or training.challenger_provider_id != "ollama"
    ):
        raise TrainingModelActivationError(
            "automatic attested activation currently requires local Ollama"
        )
    if effect_port is None:
        raise TrainingModelActivationError(
            "fresh loaded-model attestation is required before activation"
        )
    if (
        manifest_store is None
        or manifest_authority is None
        or base_prepared_model is None
        or challenger_prepared_model is None
    ):
        raise TrainingModelActivationError(
            "prepared Ollama provider manifest authority is required before activation"
        )
    _, prepared_challenger = await _bind_promotion_manifests(
        canonical=canonical,
        training=training,
        manifest_store=manifest_store,
        manifest_authority=manifest_authority,
        base_prepared_model=base_prepared_model,
        challenger_prepared_model=challenger_prepared_model,
    )

    request = _activation_probe(
        canonical=canonical,
        training=training,
    )
    gateway = AttestedTrainingCandidateGateway(
        effect_port,
        binding=training,
        expected_attestor_id=canonical.attestor_id,
        expected_attestor_sha256=canonical.attestor_sha256,
    )
    try:
        attested = await gateway.complete_attested(request)
    except ModelGatewayError as exc:
        raise TrainingModelActivationError(
            "fresh loaded-model activation attestation failed"
        ) from exc
    if (
        attested.attestation.revalidated().provider_manifest_sha256
        != prepared_challenger.provider_manifest_sha256
    ):
        raise TrainingModelActivationError(
            "fresh loaded-model provider manifest does not match prepared authority"
        )

    return _apply_promotion(
        canonical=canonical,
        settings=settings,
        expected_revision=expected_revision,
        activation_request_sha256=_activation_request_sha256(request),
        activation_attestation_sha256=_activation_attestation_sha256(attested),
    )


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
