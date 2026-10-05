from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from nika_core.experiments.contracts import (
    ExperimentDefinition,
    ExperimentSnapshot,
    ExperimentStatus,
    MetricObservation,
    MetricRule,
    PromotionPolicy,
)
from nika_core.experiments.engine import ExperimentEngine
from nika_core.experiments.repository import (
    ExperimentRepository,
    InMemoryExperimentRepository,
)
from nika_core.model_engineering import (
    EvaluationCase,
    EvaluationPurpose,
    EvaluationSet,
)
from nika_core.model_engineering.contracts import (
    BenchmarkExecutionConfig,
    ModelCandidate,
    validate_evaluation_set,
)
from nika_core.model_engineering.experiment_bridge import (
    benchmark_observations,
    build_experiment_definition,
)
from nika_core.model_gateway.contracts import ModelMessage
from nika_core.training_evaluation_binding import TrainingEvaluationBinding
from nika_core.training_evaluation_champion_execution import (
    AttestedChampionBenchmarkResult,
)
from nika_core.training_evaluation_execution import (
    AttestedChallengerBenchmarkResult,
)

_MAX_IDENTITY_BYTES = 512


class AttestedOldVsNewDecisionError(RuntimeError):
    """Safe failure while composing attested benchmarks with Experiment Engine."""


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


def _snapshot_evaluation_set(evaluation_set: EvaluationSet) -> EvaluationSet:
    if type(evaluation_set) is not EvaluationSet:
        raise TypeError("evaluation_set must be an exact EvaluationSet")
    try:
        validate_evaluation_set(evaluation_set)
        return EvaluationSet(
            evaluation_set_id=evaluation_set.evaluation_set_id,
            version=evaluation_set.version,
            provenance_ref=evaluation_set.provenance_ref,
            license_ref=evaluation_set.license_ref,
            purpose=evaluation_set.purpose,
            privacy=evaluation_set.privacy,
            cases=tuple(
                EvaluationCase(
                    case_id=case.case_id,
                    messages=tuple(
                        ModelMessage(role=message.role, content=message.content)
                        for message in case.messages
                    ),
                    expected_text=case.expected_text,
                    pass_score=case.pass_score,
                    weight=case.weight,
                )
                for case in evaluation_set.cases
            ),
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise AttestedOldVsNewDecisionError(
            "evaluation set is not canonical"
        ) from exc


def _snapshot_execution_config(
    execution_config: BenchmarkExecutionConfig,
) -> BenchmarkExecutionConfig:
    if type(execution_config) is not BenchmarkExecutionConfig:
        raise TypeError("execution_config must be an exact BenchmarkExecutionConfig")
    try:
        BenchmarkExecutionConfig.__post_init__(execution_config)
        return BenchmarkExecutionConfig(
            timeout_seconds=execution_config.timeout_seconds,
            temperature=execution_config.temperature,
            scorer_id=execution_config.scorer_id,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise AttestedOldVsNewDecisionError(
            "benchmark execution configuration is not canonical"
        ) from exc


def _snapshot_policy(policy: PromotionPolicy) -> PromotionPolicy:
    if type(policy) is not PromotionPolicy:
        raise TypeError("policy must be an exact PromotionPolicy")
    try:
        PromotionPolicy.__post_init__(policy)
        if type(policy.guardrails) is not tuple:
            raise TypeError("promotion guardrails must be a canonical tuple")
        guardrails: list[MetricRule] = []
        for rule in policy.guardrails:
            if type(rule) is not MetricRule:
                raise TypeError("promotion guardrails must use exact MetricRule values")
            MetricRule.__post_init__(rule)
            guardrails.append(
                MetricRule(
                    metric=rule.metric,
                    higher_is_better=rule.higher_is_better,
                    max_regression=rule.max_regression,
                )
            )
        return PromotionPolicy(
            primary_metric=policy.primary_metric,
            minimum_improvement=policy.minimum_improvement,
            minimum_replays=policy.minimum_replays,
            guardrails=tuple(guardrails),
            primary_higher_is_better=policy.primary_higher_is_better,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise AttestedOldVsNewDecisionError(
            "promotion policy is not canonical"
        ) from exc


def _snapshot_candidate(candidate: ModelCandidate) -> ModelCandidate:
    if type(candidate) is not ModelCandidate:
        raise TypeError("benchmark candidate must be an exact ModelCandidate")
    try:
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
        raise AttestedOldVsNewDecisionError(
            "benchmark candidate is not canonical"
        ) from exc


def _canonical_pair(
    champion: AttestedChampionBenchmarkResult,
    challenger: AttestedChallengerBenchmarkResult,
) -> tuple[
    AttestedChampionBenchmarkResult,
    AttestedChallengerBenchmarkResult,
    TrainingEvaluationBinding,
]:
    if type(champion) is not AttestedChampionBenchmarkResult:
        raise TypeError(
            "champion_benchmark must be an exact AttestedChampionBenchmarkResult"
        )
    if type(challenger) is not AttestedChallengerBenchmarkResult:
        raise TypeError(
            "challenger_benchmark must be an exact AttestedChallengerBenchmarkResult"
        )
    try:
        old = champion.revalidated()
        new = challenger.revalidated()
    except (AttributeError, TypeError, ValueError) as exc:
        raise AttestedOldVsNewDecisionError(
            "attested benchmark evidence is not canonical"
        ) from exc
    if type(old.binding) is not TrainingEvaluationBinding:
        raise AttestedOldVsNewDecisionError(
            "champion benchmark must use the canonical training binding"
        )
    if type(new.binding) is not TrainingEvaluationBinding:
        raise AttestedOldVsNewDecisionError(
            "challenger benchmark must use the canonical training binding"
        )
    binding = new.binding.revalidated()
    if old.binding.revalidated() != binding:
        raise AttestedOldVsNewDecisionError(
            "old/new benchmark evidence does not share one training binding"
        )
    if (
        old.report.candidate.candidate_id != binding.base_candidate_id
        or new.report.candidate.candidate_id != binding.challenger_candidate_id
    ):
        raise AttestedOldVsNewDecisionError(
            "old/new benchmark roles do not match the training binding"
        )
    if (
        old.attestor_id != new.attestor_id
        or old.attestor_sha256 != new.attestor_sha256
    ):
        raise AttestedOldVsNewDecisionError(
            "old/new benchmarks must use the same attestor authority"
        )
    if (
        old.report.evaluation_set_sha256 != new.report.evaluation_set_sha256
        or old.report.execution_config_sha256 != new.report.execution_config_sha256
    ):
        raise AttestedOldVsNewDecisionError(
            "old/new benchmarks are not comparable under one evaluation configuration"
        )
    return old, new, binding


def _observation_key(item: MetricObservation) -> tuple[str, str, str]:
    return item.candidate_id, item.replay_id, item.metric


def _observation_map(
    observations: tuple[MetricObservation, ...],
) -> dict[tuple[str, str, str], MetricObservation]:
    result: dict[tuple[str, str, str], MetricObservation] = {}
    for item in observations:
        if type(item) is not MetricObservation:
            raise TypeError("old/new observations must use exact MetricObservation values")
        MetricObservation.__post_init__(item)
        key = _observation_key(item)
        if key in result:
            raise AttestedOldVsNewDecisionError(
                "old/new observations contain duplicate evidence keys"
            )
        result[key] = item
    return result


def _run_engine(
    *,
    definition: ExperimentDefinition,
    observations: tuple[MetricObservation, ...],
    repository: ExperimentRepository,
) -> ExperimentSnapshot:
    """Run one clean in-memory decision to establish canonical terminal truth."""

    engine = ExperimentEngine(repository)
    engine.create(definition)
    engine.start(definition.experiment_id)
    for observation in observations:
        engine.record(definition.experiment_id, observation)
    return engine.complete(definition.experiment_id)


def _get_existing_experiment(
    repository: ExperimentRepository,
    experiment_id: str,
) -> ExperimentSnapshot | None:
    try:
        return repository.get(experiment_id)
    except KeyError:
        return None


def _validate_existing_definition(
    snapshot: ExperimentSnapshot,
    definition: ExperimentDefinition,
) -> None:
    if type(snapshot) is not ExperimentSnapshot:
        raise TypeError("experiment repository returned an invalid snapshot carrier")
    if snapshot.definition != definition:
        raise AttestedOldVsNewDecisionError(
            "persisted experiment definition conflicts with attested old/new authority"
        )


def _validate_terminal_snapshot(
    snapshot: ExperimentSnapshot,
    *,
    expected: ExperimentSnapshot,
) -> ExperimentSnapshot:
    if snapshot != expected:
        raise AttestedOldVsNewDecisionError(
            "persisted terminal experiment conflicts with canonical old/new decision"
        )
    return snapshot


def _ensure_running_experiment(
    *,
    engine: ExperimentEngine,
    repository: ExperimentRepository,
    definition: ExperimentDefinition,
) -> ExperimentSnapshot:
    experiment_id = definition.experiment_id
    snapshot = _get_existing_experiment(repository, experiment_id)
    if snapshot is None:
        try:
            engine.create(definition)
        except ValueError:
            snapshot = repository.get(experiment_id)
        else:
            snapshot = repository.get(experiment_id)
    _validate_existing_definition(snapshot, definition)
    if snapshot.status is ExperimentStatus.DRAFT:
        try:
            snapshot = engine.start(experiment_id)
        except ValueError:
            snapshot = repository.get(experiment_id)
            _validate_existing_definition(snapshot, definition)
    return snapshot


def _validate_persisted_observations(
    snapshot: ExperimentSnapshot,
    *,
    expected: dict[tuple[str, str, str], MetricObservation],
) -> dict[tuple[str, str, str], MetricObservation]:
    observed = _observation_map(snapshot.observations)
    for key, persisted in observed.items():
        wanted = expected.get(key)
        if wanted is None:
            raise AttestedOldVsNewDecisionError(
                "persisted experiment contains evidence outside attested benchmark"
            )
        if float(persisted.value) != float(wanted.value):
            raise AttestedOldVsNewDecisionError(
                "persisted experiment evidence conflicts with attested benchmark"
            )
    return observed


def _record_missing_observations(
    *,
    engine: ExperimentEngine,
    repository: ExperimentRepository,
    definition: ExperimentDefinition,
    observations: tuple[MetricObservation, ...],
) -> ExperimentSnapshot:
    experiment_id = definition.experiment_id
    expected = _observation_map(observations)
    initial = repository.get(experiment_id)
    _validate_existing_definition(initial, definition)
    if initial.status is ExperimentStatus.RUNNING:
        _validate_persisted_observations(initial, expected=expected)
    for key, item in expected.items():
        snapshot = repository.get(experiment_id)
        _validate_existing_definition(snapshot, definition)
        if snapshot.status in {ExperimentStatus.COMPLETED, ExperimentStatus.PROMOTED}:
            return snapshot
        if snapshot.status is ExperimentStatus.ROLLED_BACK:
            raise AttestedOldVsNewDecisionError(
                "rolled-back experiment cannot be reused as fresh old/new evidence"
            )
        if snapshot.status is not ExperimentStatus.RUNNING:
            raise AttestedOldVsNewDecisionError(
                "persisted experiment is not running while evidence is incomplete"
            )
        observed = _validate_persisted_observations(
            snapshot,
            expected=expected,
        )
        persisted = observed.get(key)
        if persisted is not None:
            continue
        try:
            engine.record(experiment_id, item)
        except ValueError:
            current = repository.get(experiment_id)
            _validate_existing_definition(current, definition)
            observed = _validate_persisted_observations(
                current,
                expected=expected,
            )
            persisted = observed.get(key)
            if persisted is not None:
                continue
            if current.status in {
                ExperimentStatus.COMPLETED,
                ExperimentStatus.PROMOTED,
            }:
                return current
            raise
    return repository.get(experiment_id)


def _run_engine_resumable(
    *,
    definition: ExperimentDefinition,
    observations: tuple[MetricObservation, ...],
    expected: ExperimentSnapshot,
    repository: ExperimentRepository,
) -> ExperimentSnapshot:
    """Resume the exact durable decision without replaying persisted evidence."""

    engine = ExperimentEngine(repository)
    snapshot = _ensure_running_experiment(
        engine=engine,
        repository=repository,
        definition=definition,
    )
    if snapshot.status in {ExperimentStatus.COMPLETED, ExperimentStatus.PROMOTED}:
        return _validate_terminal_snapshot(snapshot, expected=expected)
    if snapshot.status is ExperimentStatus.ROLLED_BACK:
        raise AttestedOldVsNewDecisionError(
            "rolled-back experiment cannot be reused as fresh old/new evidence"
        )
    if snapshot.status is not ExperimentStatus.RUNNING:
        raise AttestedOldVsNewDecisionError(
            "persisted experiment cannot resume from its current state"
        )

    snapshot = _record_missing_observations(
        engine=engine,
        repository=repository,
        definition=definition,
        observations=observations,
    )
    if snapshot.status in {ExperimentStatus.COMPLETED, ExperimentStatus.PROMOTED}:
        return _validate_terminal_snapshot(snapshot, expected=expected)
    if snapshot.status is ExperimentStatus.ROLLED_BACK:
        raise AttestedOldVsNewDecisionError(
            "rolled-back experiment cannot be reused as fresh old/new evidence"
        )
    try:
        snapshot = engine.complete(definition.experiment_id)
    except ValueError:
        snapshot = repository.get(definition.experiment_id)
        _validate_existing_definition(snapshot, definition)
    return _validate_terminal_snapshot(snapshot, expected=expected)


def _prepare_decision(
    *,
    experiment_id: str,
    champion_benchmark: AttestedChampionBenchmarkResult,
    challenger_benchmark: AttestedChallengerBenchmarkResult,
    evaluation_set: EvaluationSet,
    execution_config: BenchmarkExecutionConfig,
    policy: PromotionPolicy,
    permission_fingerprint: str,
) -> tuple[
    AttestedChampionBenchmarkResult,
    AttestedChallengerBenchmarkResult,
    EvaluationSet,
    BenchmarkExecutionConfig,
    PromotionPolicy,
    str,
    ExperimentDefinition,
    tuple[MetricObservation, ...],
    ExperimentSnapshot,
]:
    try:
        canonical_experiment_id = _canonical_text(
            experiment_id,
            name="experiment_id",
        )
        canonical_permission = _canonical_text(
            permission_fingerprint,
            name="permission_fingerprint",
        )
    except ValueError as exc:
        raise AttestedOldVsNewDecisionError(str(exc)) from exc

    old, new, binding = _canonical_pair(
        champion_benchmark,
        challenger_benchmark,
    )
    canonical_evaluation = _snapshot_evaluation_set(evaluation_set)
    canonical_config = _snapshot_execution_config(execution_config)
    canonical_policy = _snapshot_policy(policy)

    if canonical_evaluation.purpose is not EvaluationPurpose.HELD_OUT:
        raise AttestedOldVsNewDecisionError(
            "old/new decision requires held-out evaluation evidence"
        )
    if canonical_evaluation.content_sha256 != binding.evaluation_set_sha256:
        raise AttestedOldVsNewDecisionError(
            "evaluation set does not match the training binding"
        )
    if (
        old.report.evaluation_set_sha256 != canonical_evaluation.content_sha256
        or new.report.evaluation_set_sha256 != canonical_evaluation.content_sha256
    ):
        raise AttestedOldVsNewDecisionError(
            "old/new reports do not match the supplied evaluation set"
        )
    if (
        old.report.execution_config_sha256 != canonical_config.evidence_sha256
        or new.report.execution_config_sha256 != canonical_config.evidence_sha256
    ):
        raise AttestedOldVsNewDecisionError(
            "old/new reports do not match the supplied execution configuration"
        )

    definition = build_experiment_definition(
        experiment_id=canonical_experiment_id,
        champion=_snapshot_candidate(old.report.candidate),
        challengers=(_snapshot_candidate(new.report.candidate),),
        evaluation_set=canonical_evaluation,
        execution_config=canonical_config,
        policy=canonical_policy,
        permission_fingerprint=canonical_permission,
    )
    champion_observations = benchmark_observations(
        old.report,
        definition=definition,
        evaluation_set=canonical_evaluation,
    )
    challenger_observations = benchmark_observations(
        new.report,
        definition=definition,
        evaluation_set=canonical_evaluation,
    )
    observations = (*champion_observations, *challenger_observations)

    expected = _run_engine(
        definition=definition,
        observations=observations,
        repository=InMemoryExperimentRepository(),
    )
    return (
        old,
        new,
        canonical_evaluation,
        canonical_config,
        canonical_policy,
        canonical_permission,
        definition,
        observations,
        expected,
    )


def _definition_payload(definition: ExperimentDefinition) -> dict[str, object]:
    return {
        "experiment_id": definition.experiment_id,
        "champion": {
            "candidate_id": definition.champion.candidate_id,
            "version": definition.champion.version,
            "artifact_kind": definition.champion.artifact_kind.value,
            "artifact_ref": definition.champion.artifact_ref,
            "permission_fingerprint": definition.champion.permission_fingerprint,
        },
        "challengers": [
            {
                "candidate_id": item.candidate_id,
                "version": item.version,
                "artifact_kind": item.artifact_kind.value,
                "artifact_ref": item.artifact_ref,
                "permission_fingerprint": item.permission_fingerprint,
            }
            for item in definition.challengers
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
            "minimum_improvement": definition.policy.minimum_improvement,
            "minimum_replays": definition.policy.minimum_replays,
            "guardrails": [
                {
                    "metric": item.metric,
                    "higher_is_better": item.higher_is_better,
                    "max_regression": item.max_regression,
                }
                for item in definition.policy.guardrails
            ],
            "primary_higher_is_better": definition.policy.primary_higher_is_better,
        },
    }


def _observations_payload(
    observations: tuple[MetricObservation, ...],
) -> list[dict[str, object]]:
    return [
        {
            "candidate_id": item.candidate_id,
            "replay_id": item.replay_id,
            "metric": item.metric,
            "value": float(item.value),
        }
        for item in observations
    ]


def _hash_payload(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True, init=False)
class AttestedOldVsNewDecision:
    """Experiment Engine terminal decision from two fully-attested benchmarks."""

    snapshot: ExperimentSnapshot
    champion_benchmark: AttestedChampionBenchmarkResult
    challenger_benchmark: AttestedChallengerBenchmarkResult
    evaluation_set: EvaluationSet
    execution_config: BenchmarkExecutionConfig
    permission_fingerprint: str

    def __init_subclass__(cls, **_: object) -> None:
        raise TypeError("AttestedOldVsNewDecision cannot be subclassed")

    def _validate(self) -> tuple[
        AttestedChampionBenchmarkResult,
        AttestedChallengerBenchmarkResult,
        EvaluationSet,
        BenchmarkExecutionConfig,
        tuple[MetricObservation, ...],
        ExperimentSnapshot,
    ]:
        if type(self.snapshot) is not ExperimentSnapshot:
            raise TypeError("snapshot must be an exact ExperimentSnapshot")
        if type(self.snapshot.definition) is not ExperimentDefinition:
            raise TypeError("snapshot definition must be an exact ExperimentDefinition")
        try:
            (
                old,
                new,
                evaluation,
                config,
                _,
                permission,
                definition,
                observations,
                expected,
            ) = _prepare_decision(
                experiment_id=self.snapshot.definition.experiment_id,
                champion_benchmark=self.champion_benchmark,
                challenger_benchmark=self.challenger_benchmark,
                evaluation_set=self.evaluation_set,
                execution_config=self.execution_config,
                policy=self.snapshot.definition.policy,
                permission_fingerprint=self.permission_fingerprint,
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("old/new decision evidence is not canonical") from exc

        if permission != self.permission_fingerprint:
            raise ValueError("permission fingerprint is not canonical")
        if self.snapshot.definition != definition:
            raise ValueError("experiment definition does not match attested evidence")
        if self.snapshot.observations != observations:
            raise ValueError("experiment observations do not match attested evidence")
        if self.snapshot != expected:
            raise ValueError("experiment terminal decision does not match canonical engine")
        if self.snapshot.status not in {
            ExperimentStatus.COMPLETED,
            ExperimentStatus.PROMOTED,
        }:
            raise ValueError("old/new decision must be terminal")
        return old, new, evaluation, config, observations, expected

    def revalidated(self) -> AttestedOldVsNewDecision:
        if type(self) is not AttestedOldVsNewDecision:
            raise TypeError("decision must be an exact AttestedOldVsNewDecision")
        (
            old,
            new,
            evaluation,
            config,
            _,
            expected,
        ) = self._validate()
        return _build_decision(
            snapshot=expected,
            champion_benchmark=old,
            challenger_benchmark=new,
            evaluation_set=evaluation,
            execution_config=config,
            permission_fingerprint=self.permission_fingerprint,
        )

    def evidence_payload(self) -> dict[str, object]:
        decision = self.revalidated()
        observations = decision.snapshot.observations
        definition = decision.snapshot.definition
        return {
            "schema": "nika-attested-old-vs-new-decision-v1",
            "experiment_id": definition.experiment_id,
            "experiment_status": decision.snapshot.status.value,
            "selected_candidate_id": decision.snapshot.selected_candidate_id,
            "previous_champion_id": decision.snapshot.previous_champion_id,
            "binding_sha256": decision.challenger_benchmark.binding.binding_sha256,
            "champion_benchmark_evidence_sha256": (
                decision.champion_benchmark.evidence_sha256
            ),
            "challenger_benchmark_evidence_sha256": (
                decision.challenger_benchmark.evidence_sha256
            ),
            "evaluation_set_sha256": decision.evaluation_set.content_sha256,
            "execution_config_sha256": decision.execution_config.evidence_sha256,
            "experiment_definition_sha256": _hash_payload(
                _definition_payload(definition)
            ),
            "observations_sha256": _hash_payload(
                _observations_payload(observations)
            ),
            "observation_count": len(observations),
            "permission_fingerprint_sha256": hashlib.sha256(
                decision.permission_fingerprint.encode("utf-8")
            ).hexdigest(),
            "model_activation_performed": False,
        }

    @property
    def evidence_sha256(self) -> str:
        return _hash_payload(self.evidence_payload())


def _build_decision(
    *,
    snapshot: ExperimentSnapshot,
    champion_benchmark: AttestedChampionBenchmarkResult,
    challenger_benchmark: AttestedChallengerBenchmarkResult,
    evaluation_set: EvaluationSet,
    execution_config: BenchmarkExecutionConfig,
    permission_fingerprint: str,
) -> AttestedOldVsNewDecision:
    result = object.__new__(AttestedOldVsNewDecision)
    object.__setattr__(result, "snapshot", snapshot)
    object.__setattr__(result, "champion_benchmark", champion_benchmark)
    object.__setattr__(result, "challenger_benchmark", challenger_benchmark)
    object.__setattr__(result, "evaluation_set", evaluation_set)
    object.__setattr__(result, "execution_config", execution_config)
    object.__setattr__(result, "permission_fingerprint", permission_fingerprint)
    result._validate()
    return result


def evaluate_attested_old_vs_new(
    *,
    experiment_id: str,
    champion_benchmark: AttestedChampionBenchmarkResult,
    challenger_benchmark: AttestedChallengerBenchmarkResult,
    evaluation_set: EvaluationSet,
    execution_config: BenchmarkExecutionConfig,
    policy: PromotionPolicy,
    permission_fingerprint: str,
    repository: ExperimentRepository,
) -> AttestedOldVsNewDecision:
    """Feed verified old/new reports into the existing Experiment Engine.

    All attestation, evaluation, execution-config and bridge validation completes
    before the caller-supplied repository receives its first durable mutation.
    ExperimentStatus.PROMOTED is only the existing Engine decision; this function
    has no model-activation capability.
    """

    (
        old,
        new,
        canonical_evaluation,
        canonical_config,
        _,
        canonical_permission,
        definition,
        observations,
        expected,
    ) = _prepare_decision(
        experiment_id=experiment_id,
        champion_benchmark=champion_benchmark,
        challenger_benchmark=challenger_benchmark,
        evaluation_set=evaluation_set,
        execution_config=execution_config,
        policy=policy,
        permission_fingerprint=permission_fingerprint,
    )
    completed = _run_engine_resumable(
        definition=definition,
        observations=observations,
        expected=expected,
        repository=repository,
    )
    return _build_decision(
        snapshot=completed,
        champion_benchmark=old,
        challenger_benchmark=new,
        evaluation_set=canonical_evaluation,
        execution_config=canonical_config,
        permission_fingerprint=canonical_permission,
    )


__all__ = [
    "AttestedOldVsNewDecision",
    "AttestedOldVsNewDecisionError",
    "evaluate_attested_old_vs_new",
]
