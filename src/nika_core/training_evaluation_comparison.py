from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from nika_core.experiments import (
    ExperimentDefinition,
    ExperimentEngine,
    ExperimentRepository,
    ExperimentSnapshot,
    ExperimentStatus,
    MetricObservation,
    PromotionPolicy,
)
from nika_core.model_engineering import (
    BenchmarkExecutionConfig,
    EvaluationSet,
    benchmark_observations,
    build_experiment_definition,
)
from nika_core.model_engineering.contracts import validate_evaluation_set
from nika_core.training_evaluation_champion_execution import AttestedChampionBenchmarkResult
from nika_core.training_evaluation_execution import AttestedChallengerBenchmarkResult


def _observation_key(item: MetricObservation) -> tuple[str, str, str]:
    return item.candidate_id, item.replay_id, item.metric


def _observation_map(
    observations: tuple[MetricObservation, ...],
) -> dict[tuple[str, str, str], MetricObservation]:
    result: dict[tuple[str, str, str], MetricObservation] = {}
    for item in observations:
        if type(item) is not MetricObservation:
            raise TypeError("comparison observations must use exact MetricObservation values")
        MetricObservation.__post_init__(item)
        key = _observation_key(item)
        if key in result:
            raise ValueError("comparison observations contain duplicate evidence keys")
        result[key] = item
    return result


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
            "minimum_improvement": float(definition.policy.minimum_improvement),
            "minimum_replays": definition.policy.minimum_replays,
            "primary_higher_is_better": definition.policy.primary_higher_is_better,
            "guardrails": [
                {
                    "metric": rule.metric,
                    "higher_is_better": rule.higher_is_better,
                    "max_regression": float(rule.max_regression),
                }
                for rule in definition.policy.guardrails
            ],
        },
    }


def _definition_sha256(definition: ExperimentDefinition) -> str:
    encoded = json.dumps(
        _definition_payload(definition),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _observations_sha256(observations: tuple[MetricObservation, ...]) -> str:
    payload = [
        {
            "candidate_id": item.candidate_id,
            "replay_id": item.replay_id,
            "metric": item.metric,
            "value": float(item.value),
        }
        for item in sorted(observations, key=_observation_key)
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_definition(snapshot: ExperimentSnapshot, definition: ExperimentDefinition) -> None:
    if type(snapshot) is not ExperimentSnapshot:
        raise TypeError("experiment repository returned an invalid snapshot carrier")
    if snapshot.definition != definition:
        raise ValueError("existing experiment definition does not match attested comparison")


def _validate_terminal_snapshot(
    snapshot: ExperimentSnapshot,
    *,
    definition: ExperimentDefinition,
    expected_observations: tuple[MetricObservation, ...],
) -> ExperimentSnapshot:
    _validate_definition(snapshot, definition)
    if snapshot.status not in {ExperimentStatus.COMPLETED, ExperimentStatus.PROMOTED}:
        raise ValueError("attested comparison did not reach a terminal experiment state")
    expected = _observation_map(expected_observations)
    observed = _observation_map(snapshot.observations)
    if set(observed) != set(expected):
        raise ValueError("terminal experiment observation coverage is inconsistent")
    for key, item in expected.items():
        if float(observed[key].value) != float(item.value):
            raise ValueError("terminal experiment observation value is inconsistent")
    champion_id = definition.champion.candidate_id
    challenger_id = definition.challengers[0].candidate_id
    if snapshot.previous_champion_id != champion_id:
        raise ValueError("terminal experiment previous-champion evidence is inconsistent")
    if snapshot.status is ExperimentStatus.PROMOTED:
        if snapshot.selected_candidate_id != challenger_id:
            raise ValueError("promoted experiment selected-candidate evidence is inconsistent")
    elif snapshot.selected_candidate_id != champion_id:
        raise ValueError("completed experiment selected-candidate evidence is inconsistent")
    return snapshot


def _get_existing(
    repository: ExperimentRepository,
    experiment_id: str,
) -> ExperimentSnapshot | None:
    try:
        return repository.get(experiment_id)
    except KeyError:
        return None


def _ensure_created_and_running(
    *,
    engine: ExperimentEngine,
    repository: ExperimentRepository,
    definition: ExperimentDefinition,
) -> ExperimentSnapshot:
    experiment_id = definition.experiment_id
    snapshot = _get_existing(repository, experiment_id)
    if snapshot is None:
        try:
            engine.create(definition)
        except ValueError:
            snapshot = repository.get(experiment_id)
            _validate_definition(snapshot, definition)
        else:
            snapshot = repository.get(experiment_id)
    _validate_definition(snapshot, definition)
    if snapshot.status is ExperimentStatus.DRAFT:
        try:
            snapshot = engine.start(experiment_id)
        except ValueError:
            snapshot = repository.get(experiment_id)
            _validate_definition(snapshot, definition)
    return snapshot


def _record_expected_observations(
    *,
    engine: ExperimentEngine,
    repository: ExperimentRepository,
    definition: ExperimentDefinition,
    observations: tuple[MetricObservation, ...],
) -> ExperimentSnapshot:
    experiment_id = definition.experiment_id
    expected = _observation_map(observations)
    for key, item in expected.items():
        snapshot = repository.get(experiment_id)
        _validate_definition(snapshot, definition)
        if snapshot.status in {ExperimentStatus.COMPLETED, ExperimentStatus.PROMOTED}:
            return snapshot
        if snapshot.status is not ExperimentStatus.RUNNING:
            raise ValueError("experiment is not running while comparison evidence is incomplete")
        existing = _observation_map(snapshot.observations)
        if key in existing:
            if float(existing[key].value) != float(item.value):
                raise ValueError("persisted experiment evidence conflicts with attested benchmark")
            continue
        try:
            engine.record(experiment_id, item)
        except ValueError:
            current = repository.get(experiment_id)
            _validate_definition(current, definition)
            persisted = _observation_map(current.observations).get(key)
            if persisted is None or float(persisted.value) != float(item.value):
                raise
    return repository.get(experiment_id)


def _finish_experiment(
    *,
    engine: ExperimentEngine,
    repository: ExperimentRepository,
    definition: ExperimentDefinition,
    expected_observations: tuple[MetricObservation, ...],
) -> ExperimentSnapshot:
    experiment_id = definition.experiment_id
    snapshot = repository.get(experiment_id)
    _validate_definition(snapshot, definition)
    if snapshot.status in {ExperimentStatus.COMPLETED, ExperimentStatus.PROMOTED}:
        return _validate_terminal_snapshot(
            snapshot,
            definition=definition,
            expected_observations=expected_observations,
        )
    if snapshot.status is ExperimentStatus.ROLLED_BACK:
        raise ValueError("rolled-back experiment cannot be reused as fresh comparison evidence")
    if snapshot.status is not ExperimentStatus.RUNNING:
        raise ValueError("experiment cannot complete from its current state")
    try:
        snapshot = engine.complete(experiment_id)
    except ValueError:
        snapshot = repository.get(experiment_id)
    return _validate_terminal_snapshot(
        snapshot,
        definition=definition,
        expected_observations=expected_observations,
    )


@dataclass(frozen=True, slots=True, init=False)
class AttestedTrainingComparisonResult:
    experiment_snapshot: ExperimentSnapshot
    training_binding_sha256: str
    champion_binding_sha256: str
    champion_benchmark_sha256: str
    challenger_benchmark_sha256: str
    definition_sha256: str
    observations_sha256: str

    def __init_subclass__(cls, **_: object) -> None:
        raise TypeError("AttestedTrainingComparisonResult cannot be subclassed")

    def _validate(self) -> None:
        if type(self.experiment_snapshot) is not ExperimentSnapshot:
            raise TypeError("experiment_snapshot must be an exact ExperimentSnapshot")
        for value, name in (
            (self.training_binding_sha256, "training_binding_sha256"),
            (self.champion_binding_sha256, "champion_binding_sha256"),
            (self.champion_benchmark_sha256, "champion_benchmark_sha256"),
            (self.challenger_benchmark_sha256, "challenger_benchmark_sha256"),
            (self.definition_sha256, "definition_sha256"),
            (self.observations_sha256, "observations_sha256"),
        ):
            if type(value) is not str or len(value) != 64:
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
            if any(character not in "0123456789abcdef" for character in value):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
        if _definition_sha256(self.experiment_snapshot.definition) != self.definition_sha256:
            raise ValueError("experiment definition evidence changed")
        if _observations_sha256(self.experiment_snapshot.observations) != self.observations_sha256:
            raise ValueError("experiment observation evidence changed")
        if self.experiment_snapshot.status not in {
            ExperimentStatus.COMPLETED,
            ExperimentStatus.PROMOTED,
        }:
            raise ValueError("comparison result requires a terminal experiment snapshot")

    def revalidated(self) -> AttestedTrainingComparisonResult:
        if type(self) is not AttestedTrainingComparisonResult:
            raise TypeError("result must be an exact AttestedTrainingComparisonResult")
        try:
            self._validate()
            return _build_result(
                experiment_snapshot=self.experiment_snapshot,
                training_binding_sha256=self.training_binding_sha256,
                champion_binding_sha256=self.champion_binding_sha256,
                champion_benchmark_sha256=self.champion_benchmark_sha256,
                challenger_benchmark_sha256=self.challenger_benchmark_sha256,
            )
        except AttributeError as exc:
            raise ValueError("attested comparison result fields are incomplete") from exc

    def evidence_payload(self) -> dict[str, object]:
        result = self.revalidated()
        snapshot = result.experiment_snapshot
        return {
            "schema": "nika-attested-training-comparison-v1",
            "experiment_id": snapshot.definition.experiment_id,
            "experiment_status": snapshot.status.value,
            "selected_candidate_id": snapshot.selected_candidate_id,
            "previous_champion_id": snapshot.previous_champion_id,
            "training_binding_sha256": result.training_binding_sha256,
            "champion_binding_sha256": result.champion_binding_sha256,
            "champion_benchmark_sha256": result.champion_benchmark_sha256,
            "challenger_benchmark_sha256": result.challenger_benchmark_sha256,
            "definition_sha256": result.definition_sha256,
            "observations_sha256": result.observations_sha256,
            "observation_count": len(snapshot.observations),
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
    experiment_snapshot: ExperimentSnapshot,
    training_binding_sha256: str,
    champion_binding_sha256: str,
    champion_benchmark_sha256: str,
    challenger_benchmark_sha256: str,
) -> AttestedTrainingComparisonResult:
    result = object.__new__(AttestedTrainingComparisonResult)
    object.__setattr__(result, "experiment_snapshot", experiment_snapshot)
    object.__setattr__(result, "training_binding_sha256", training_binding_sha256)
    object.__setattr__(result, "champion_binding_sha256", champion_binding_sha256)
    object.__setattr__(result, "champion_benchmark_sha256", champion_benchmark_sha256)
    object.__setattr__(result, "challenger_benchmark_sha256", challenger_benchmark_sha256)
    object.__setattr__(
        result,
        "definition_sha256",
        _definition_sha256(experiment_snapshot.definition),
    )
    object.__setattr__(
        result,
        "observations_sha256",
        _observations_sha256(experiment_snapshot.observations),
    )
    result._validate()
    return result


def run_attested_old_vs_new_comparison(
    *,
    champion_result: AttestedChampionBenchmarkResult,
    challenger_result: AttestedChallengerBenchmarkResult,
    evaluation_set: EvaluationSet,
    execution_config: BenchmarkExecutionConfig,
    policy: PromotionPolicy,
    permission_fingerprint: str,
    experiment_id: str,
    repository: ExperimentRepository,
) -> AttestedTrainingComparisonResult:
    """Persist exact attested old/new evidence through the canonical Experiment Engine.

    A PROMOTED experiment status is selection evidence only. This function has no
    ModelGateway reconfiguration, deployment, download, source-mutation, or external
    effect authority.
    """

    try:
        champion = champion_result.revalidated()
        challenger = challenger_result.revalidated()
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("attested benchmark evidence must be canonical") from exc
    try:
        validate_evaluation_set(evaluation_set)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("evaluation set must be canonical") from exc
    if type(execution_config) is not BenchmarkExecutionConfig:
        raise TypeError("execution_config must be an exact BenchmarkExecutionConfig")
    BenchmarkExecutionConfig.__post_init__(execution_config)
    training_binding = challenger.binding.revalidated()
    champion_binding = champion.binding.revalidated()
    if champion_binding.training_binding_sha256 != training_binding.binding_sha256:
        raise ValueError("champion and challenger do not share one training authority")
    if champion_binding.job_id != training_binding.job_id:
        raise ValueError("champion and challenger job identity differs")
    if (
        champion_binding.candidate_id != training_binding.base_candidate_id
        or champion_binding.artifact_sha256 != training_binding.base_sha256
        or champion.report.candidate.candidate_id != training_binding.base_candidate_id
    ):
        raise ValueError("champion benchmark does not match training base authority")
    if challenger.report.candidate.candidate_id != training_binding.challenger_candidate_id:
        raise ValueError("challenger benchmark does not match training output authority")
    if evaluation_set.content_sha256 != training_binding.evaluation_set_sha256:
        raise ValueError("evaluation set does not match training authority")
    if (
        champion.report.evaluation_set_sha256 != evaluation_set.content_sha256
        or challenger.report.evaluation_set_sha256 != evaluation_set.content_sha256
    ):
        raise ValueError("old/new benchmarks do not share the exact held-out evaluation set")
    expected_config_sha256 = execution_config.evidence_sha256
    if (
        champion.report.execution_config_sha256 != expected_config_sha256
        or challenger.report.execution_config_sha256 != expected_config_sha256
    ):
        raise ValueError("old/new benchmarks do not share the exact execution configuration")

    definition = build_experiment_definition(
        experiment_id=experiment_id,
        champion=champion.report.candidate,
        challengers=(challenger.report.candidate,),
        evaluation_set=evaluation_set,
        execution_config=execution_config,
        policy=policy,
        permission_fingerprint=permission_fingerprint,
    )
    observations = (
        *benchmark_observations(
            champion.report,
            definition=definition,
            evaluation_set=evaluation_set,
        ),
        *benchmark_observations(
            challenger.report,
            definition=definition,
            evaluation_set=evaluation_set,
        ),
    )
    _observation_map(observations)
    engine = ExperimentEngine(repository)
    snapshot = _ensure_created_and_running(
        engine=engine,
        repository=repository,
        definition=definition,
    )
    if snapshot.status is ExperimentStatus.ROLLED_BACK:
        raise ValueError("rolled-back experiment cannot be reused as fresh comparison evidence")
    if snapshot.status not in {ExperimentStatus.COMPLETED, ExperimentStatus.PROMOTED}:
        snapshot = _record_expected_observations(
            engine=engine,
            repository=repository,
            definition=definition,
            observations=observations,
        )
    if snapshot.status not in {ExperimentStatus.COMPLETED, ExperimentStatus.PROMOTED}:
        snapshot = _finish_experiment(
            engine=engine,
            repository=repository,
            definition=definition,
            expected_observations=observations,
        )
    else:
        snapshot = _validate_terminal_snapshot(
            snapshot,
            definition=definition,
            expected_observations=observations,
        )
    return _build_result(
        experiment_snapshot=snapshot,
        training_binding_sha256=training_binding.binding_sha256,
        champion_binding_sha256=champion_binding.binding_sha256,
        champion_benchmark_sha256=champion.evidence_sha256,
        challenger_benchmark_sha256=challenger.evidence_sha256,
    )


__all__ = [
    "AttestedTrainingComparisonResult",
    "run_attested_old_vs_new_comparison",
]
