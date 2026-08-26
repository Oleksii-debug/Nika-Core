from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any

from nika_core.experiments.contracts import (
    ArtifactKind,
    ExperimentDefinition,
    ExperimentSnapshot,
    ExperimentStatus,
    MetricObservation,
    MetricRule,
    PromotionPolicy,
    ReplayCase,
    StrategyRef,
)
from nika_core.experiments.engine import ExperimentEngine
from nika_core.experiments.repository import ExperimentRepository
from nika_core.model_gateway.contracts import PrivacyClass, ProviderKind

from .contracts import (
    CaseMeasurement,
    EvaluationCase,
    ModelCandidate,
    ModelExperimentSpec,
    ModelRecommendation,
)

_CANDIDATE_REF_PREFIX = "nika-model-candidate:v1:"
_CASE_REF_PREFIX = "nika-model-eval-case:v1:"


class ModelEngineeringLab:
    """Thin Model Engineering adapter over the existing durable Experiment Engine.

    This service records evaluation evidence and returns recommendations only. It has
    deliberately no production-routing/config mutation surface.
    """

    def __init__(self, repository: ExperimentRepository) -> None:
        self._repository = repository
        self._engine = ExperimentEngine(repository)

    def create(self, spec: ModelExperimentSpec) -> ExperimentSnapshot:
        definition = ExperimentDefinition(
            experiment_id=spec.experiment_id,
            champion=self._strategy_ref(spec.champion),
            challengers=tuple(self._strategy_ref(item) for item in spec.challengers),
            replays=tuple(self._replay_case(item) for item in spec.cases),
            policy=PromotionPolicy(
                primary_metric=spec.primary_metric.name,
                minimum_improvement=float(spec.minimum_improvement),
                minimum_replays=len(spec.cases),
                guardrails=tuple(
                    MetricRule(
                        metric=item.name,
                        higher_is_better=item.higher_is_better,
                        max_regression=float(item.max_regression),
                    )
                    for item in spec.guardrails
                ),
                primary_higher_is_better=spec.primary_metric.higher_is_better,
            ),
        )
        try:
            return self._engine.create(definition)
        except ValueError:
            try:
                current = self._repository.get(spec.experiment_id)
            except KeyError:
                raise
            if current.definition != definition:
                raise ValueError(
                    "experiment already exists with different immutable definition: "
                    f"{spec.experiment_id}"
                ) from None
            return current

    def start(self, experiment_id: str) -> ExperimentSnapshot:
        snapshot = self._repository.get(experiment_id)
        if snapshot.status is ExperimentStatus.RUNNING:
            return snapshot
        if snapshot.status is not ExperimentStatus.DRAFT:
            raise ValueError("only draft model experiments can start")
        return self._engine.start(experiment_id)

    def record_measurement(
        self, experiment_id: str, measurement: CaseMeasurement
    ) -> ExperimentSnapshot:
        snapshot = self._repository.get(experiment_id)
        if snapshot.status is not ExperimentStatus.RUNNING:
            raise ValueError("measurements require a running model experiment")
        candidates = (snapshot.definition.champion, *snapshot.definition.challengers)
        candidate_map = {item.candidate_id: item for item in candidates}
        strategy = candidate_map.get(measurement.candidate_id)
        if strategy is None:
            raise ValueError("measurement references an unknown candidate")
        self.decode_candidate(strategy)
        replay_map = {item.replay_id: item for item in snapshot.definition.replays}
        replay = replay_map.get(measurement.case_id)
        if replay is None:
            raise ValueError("measurement references an unknown evaluation case")
        self.decode_case(replay)
        expected_metrics = (
            snapshot.definition.policy.primary_metric,
            *(item.metric for item in snapshot.definition.policy.guardrails),
        )
        supplied = {item.name: float(item.value) for item in measurement.metrics}
        if set(supplied) != set(expected_metrics):
            missing = sorted(set(expected_metrics) - set(supplied))
            extra = sorted(set(supplied) - set(expected_metrics))
            raise ValueError(
                "measurement metric set does not match experiment policy: "
                f"missing={missing}, extra={extra}"
            )

        current = snapshot
        for metric in expected_metrics:
            observation = MetricObservation(
                candidate_id=measurement.candidate_id,
                replay_id=measurement.case_id,
                metric=metric,
                value=supplied[metric],
            )
            current = self._record_idempotent(experiment_id, observation)
        return current

    def complete(self, experiment_id: str) -> ModelRecommendation:
        snapshot = self._repository.get(experiment_id)
        if snapshot.status is ExperimentStatus.RUNNING:
            self._engine.complete(experiment_id)
        elif snapshot.status not in {ExperimentStatus.COMPLETED, ExperimentStatus.PROMOTED}:
            raise ValueError("only running or already-completed model experiments can complete")
        return self.recommendation(experiment_id)

    def rollback(self, experiment_id: str) -> ModelRecommendation:
        snapshot = self._repository.get(experiment_id)
        if snapshot.status is ExperimentStatus.PROMOTED:
            self._engine.rollback(experiment_id)
        elif snapshot.status is not ExperimentStatus.ROLLED_BACK:
            raise ValueError("rollback requires a promoted model experiment")
        return self.recommendation(experiment_id)

    def recommendation(self, experiment_id: str) -> ModelRecommendation:
        snapshot = self._repository.get(experiment_id)
        if snapshot.status not in {
            ExperimentStatus.COMPLETED,
            ExperimentStatus.PROMOTED,
            ExperimentStatus.ROLLED_BACK,
        }:
            raise ValueError("model recommendation requires a completed experiment")
        selected_id = snapshot.selected_candidate_id
        previous_id = snapshot.previous_champion_id
        if selected_id is None or previous_id is None:
            raise RuntimeError("completed experiment lacks selection evidence")
        candidates = (
            snapshot.definition.champion,
            *snapshot.definition.challengers,
        )
        try:
            selected = next(item for item in candidates if item.candidate_id == selected_id)
        except StopIteration as exc:
            raise RuntimeError("selected candidate is absent from immutable definition") from exc
        candidate = self.decode_candidate(selected)
        if candidate.candidate_id != selected_id:
            raise RuntimeError("candidate manifest identity does not match selected candidate")
        return ModelRecommendation(
            experiment_id=experiment_id,
            candidate_id=selected_id,
            candidate_manifest_sha256=selected.version,
            evidence_sha256=self._evidence_digest(snapshot),
            previous_champion_id=previous_id,
        )

    @classmethod
    def decode_candidate(cls, strategy: StrategyRef) -> ModelCandidate:
        if strategy.artifact_kind is not ArtifactKind.CONFIG:
            raise ValueError("model candidate must use CONFIG artifact kind")
        payload = cls._decode_ref(strategy.artifact_ref, _CANDIDATE_REF_PREFIX)
        digest = cls._digest_payload(payload)
        if digest != strategy.version:
            raise ValueError("model candidate manifest digest mismatch")
        cls._require_manifest_keys(
            payload,
            {
                "candidate_id",
                "provider_id",
                "provider_kind",
                "model_id",
                "model_version",
                "source_ref",
                "license_ref",
                "permission_fingerprint",
                "supports_private_data",
                "artifact_sha256",
            },
        )
        supports_private_data = payload["supports_private_data"]
        if not isinstance(supports_private_data, bool):
            raise ValueError("candidate supports_private_data evidence must be boolean")
        raw_artifact_sha256 = payload["artifact_sha256"]
        if raw_artifact_sha256 is not None and not isinstance(raw_artifact_sha256, str):
            raise ValueError("candidate artifact_sha256 evidence must be text or null")
        candidate = ModelCandidate(
            candidate_id=cls._manifest_text(payload, "candidate_id"),
            provider_id=cls._manifest_text(payload, "provider_id"),
            provider_kind=ProviderKind(cls._manifest_text(payload, "provider_kind")),
            model_id=cls._manifest_text(payload, "model_id"),
            model_version=cls._manifest_text(payload, "model_version"),
            source_ref=cls._manifest_text(payload, "source_ref"),
            license_ref=cls._manifest_text(payload, "license_ref"),
            permission_fingerprint=cls._manifest_text(payload, "permission_fingerprint"),
            supports_private_data=supports_private_data,
            artifact_sha256=raw_artifact_sha256,
        )
        if candidate.permission_fingerprint != strategy.permission_fingerprint:
            raise ValueError("candidate permission evidence mismatch")
        return candidate

    @classmethod
    def decode_case(cls, replay: ReplayCase) -> EvaluationCase:
        payload = cls._decode_ref(replay.dataset_ref, _CASE_REF_PREFIX)
        digest = cls._digest_payload(payload)
        if digest != replay.dataset_version:
            raise ValueError("evaluation case manifest digest mismatch")
        cls._require_manifest_keys(
            payload,
            {"case_id", "dataset_ref", "dataset_version", "dataset_sha256", "privacy"},
        )
        case = EvaluationCase(
            case_id=cls._manifest_text(payload, "case_id"),
            dataset_ref=cls._manifest_text(payload, "dataset_ref"),
            dataset_version=cls._manifest_text(payload, "dataset_version"),
            dataset_sha256=cls._manifest_text(payload, "dataset_sha256"),
            privacy=PrivacyClass(cls._manifest_text(payload, "privacy")),
        )
        if case.case_id != replay.replay_id:
            raise ValueError("evaluation case identity mismatch")
        return case

    def _record_idempotent(
        self, experiment_id: str, observation: MetricObservation
    ) -> ExperimentSnapshot:
        key = (observation.candidate_id, observation.replay_id, observation.metric)
        for _ in range(3):
            current = self._repository.get(experiment_id)
            existing = {
                (item.candidate_id, item.replay_id, item.metric): item
                for item in current.observations
            }
            prior = existing.get(key)
            if prior is not None:
                if float(prior.value) != float(observation.value):
                    raise ValueError("recorded model evaluation evidence is immutable")
                return current
            try:
                return self._engine.record(experiment_id, observation)
            except ValueError as exc:
                refreshed = self._repository.get(experiment_id)
                refreshed_map = {
                    (item.candidate_id, item.replay_id, item.metric): item
                    for item in refreshed.observations
                }
                prior = refreshed_map.get(key)
                if prior is not None:
                    if float(prior.value) != float(observation.value):
                        raise ValueError("recorded model evaluation evidence is immutable")
                    return refreshed
                if refreshed.status is not ExperimentStatus.RUNNING:
                    raise exc
        raise RuntimeError("concurrent model evaluation update did not converge")

    @classmethod
    def _strategy_ref(cls, candidate: ModelCandidate) -> StrategyRef:
        payload = {
            "candidate_id": candidate.candidate_id,
            "provider_id": candidate.provider_id,
            "provider_kind": candidate.provider_kind.value,
            "model_id": candidate.model_id,
            "model_version": candidate.model_version,
            "source_ref": candidate.source_ref,
            "license_ref": candidate.license_ref,
            "permission_fingerprint": candidate.permission_fingerprint,
            "supports_private_data": candidate.supports_private_data,
            "artifact_sha256": candidate.artifact_sha256,
        }
        return StrategyRef(
            candidate_id=candidate.candidate_id,
            version=cls._digest_payload(payload),
            artifact_kind=ArtifactKind.CONFIG,
            artifact_ref=cls._encode_ref(_CANDIDATE_REF_PREFIX, payload),
            permission_fingerprint=candidate.permission_fingerprint,
        )

    @classmethod
    def _replay_case(cls, case: EvaluationCase) -> ReplayCase:
        payload = {
            "case_id": case.case_id,
            "dataset_ref": case.dataset_ref,
            "dataset_version": case.dataset_version,
            "dataset_sha256": case.dataset_sha256,
            "privacy": case.privacy.value,
        }
        return ReplayCase(
            replay_id=case.case_id,
            dataset_ref=cls._encode_ref(_CASE_REF_PREFIX, payload),
            dataset_version=cls._digest_payload(payload),
        )

    @staticmethod
    def _manifest_text(payload: dict[str, Any], key: str) -> str:
        value = payload[key]
        if not isinstance(value, str):
            raise ValueError(f"model engineering manifest field {key!r} must be text")
        return value

    @staticmethod
    def _require_manifest_keys(payload: dict[str, Any], expected: set[str]) -> None:
        actual = set(payload)
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise ValueError(
                "model engineering manifest schema mismatch: "
                f"missing={missing}, extra={extra}"
            )

    @staticmethod
    def _encode_ref(prefix: str, payload: dict[str, Any]) -> str:
        return prefix + json.dumps(payload, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _decode_ref(value: str, prefix: str) -> dict[str, Any]:
        if not value.startswith(prefix):
            raise ValueError("unsupported model engineering manifest reference")
        try:
            payload = json.loads(value[len(prefix) :])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid model engineering manifest reference") from exc
        if not isinstance(payload, dict):
            raise ValueError("model engineering manifest must be an object")
        return payload

    @staticmethod
    def _digest_payload(payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _evidence_digest(cls, snapshot: ExperimentSnapshot) -> str:
        definition = snapshot.definition
        payload = {
            "experiment_id": definition.experiment_id,
            "status": snapshot.status.value,
            "candidates": [
                {
                    "candidate_id": item.candidate_id,
                    "version": item.version,
                    "artifact_kind": item.artifact_kind.value,
                    "artifact_ref": item.artifact_ref,
                    "permission_fingerprint": item.permission_fingerprint,
                }
                for item in (definition.champion, *definition.challengers)
            ],
            "replays": [
                {
                    "replay_id": item.replay_id,
                    "dataset_ref": item.dataset_ref,
                    "dataset_version": item.dataset_version,
                }
                for item in definition.replays
            ],
            "policy": {
                "primary_metric": definition.policy.primary_metric,
                "primary_higher_is_better": definition.policy.primary_higher_is_better,
                "minimum_improvement": definition.policy.minimum_improvement,
                "minimum_replays": definition.policy.minimum_replays,
                "guardrails": [asdict(item) for item in definition.policy.guardrails],
            },
            "observations": [
                {
                    "candidate_id": item.candidate_id,
                    "replay_id": item.replay_id,
                    "metric": item.metric,
                    "value": float(item.value),
                }
                for item in sorted(
                    snapshot.observations,
                    key=lambda item: (item.candidate_id, item.replay_id, item.metric),
                )
            ],
            "selected_candidate_id": snapshot.selected_candidate_id,
            "previous_champion_id": snapshot.previous_champion_id,
        }
        return cls._digest_payload(payload)
