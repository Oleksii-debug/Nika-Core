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
    InMemoryExperimentRepository,
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
from nika_core.training_evaluation_binding import TrainingEvaluationBinding
from nika_core.training_evaluation_champion_execution import (
    AttestedChampionBenchmarkResult,
)
from nika_core.training_evaluation_execution import AttestedChallengerBenchmarkResult


def _validate_shared_attestor(
    champion: AttestedChampionBenchmarkResult,
    challenger: AttestedChallengerBenchmarkResult,
) -> None:
    if (
        champion.attestor_id != challenger.attestor_id
        or champion.attestor_sha256 != challenger.attestor_sha256
    ):
        raise ValueError("old/new benchmarks do not share one attestor authority")


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


def experiment_snapshot_evidence_identity(
    snapshot: ExperimentSnapshot,
) -> tuple[str, str, int]:
    """Return the canonical durable identity of one Experiment snapshot."""

    if type(snapshot) is not ExperimentSnapshot:
        raise TypeError("snapshot must be an exact ExperimentSnapshot")
    if type(snapshot.definition) is not ExperimentDefinition:
        raise TypeError("snapshot definition must be an exact ExperimentDefinition")
    ExperimentDefinition.__post_init__(snapshot.definition)
    if type(snapshot.observations) is not tuple:
        raise TypeError("snapshot observations must use a canonical tuple")
    _observation_map(snapshot.observations)
    return (
        _definition_sha256(snapshot.definition),
        _observations_sha256(snapshot.observations),
        len(snapshot.observations),
    )


def _validate_definition(snapshot: ExperimentSnapshot, definition: ExperimentDefinition) -> None:
    if type(snapshot) is not ExperimentSnapshot:
        raise TypeError("experiment repository returned an invalid snapshot carrier")
    if snapshot.definition != definition:
        raise ValueError("existing experiment definition does not match attested comparison")


def _run_canonical_terminal(
    *,
    definition: ExperimentDefinition,
    observations: tuple[MetricObservation, ...],
) -> ExperimentSnapshot:
    repository = InMemoryExperimentRepository()
    engine = ExperimentEngine(repository)
    engine.create(definition)
    engine.start(definition.experiment_id)
    for observation in observations:
        engine.record(definition.experiment_id, observation)
    return engine.complete(definition.experiment_id)


def _validate_recoverable_snapshot(snapshot: ExperimentSnapshot) -> None:
    if snapshot.status is ExperimentStatus.DRAFT:
        if (
            snapshot.observations != ()
            or snapshot.selected_candidate_id is not None
            or snapshot.previous_champion_id is not None
        ):
            raise ValueError("draft experiment contains durable comparison evidence")
    elif snapshot.status is ExperimentStatus.RUNNING:
        if (
            snapshot.selected_candidate_id is not None
            or snapshot.previous_champion_id is not None
        ):
            raise ValueError("running experiment contains terminal decision fields")


def _validate_persisted_observations(
    snapshot: ExperimentSnapshot,
    *,
    expected: dict[tuple[str, str, str], MetricObservation],
) -> dict[tuple[str, str, str], MetricObservation]:
    if type(snapshot.observations) is not tuple:
        raise TypeError("persisted comparison observations must use a canonical tuple")
    observed = _observation_map(snapshot.observations)
    for key, persisted in observed.items():
        wanted = expected.get(key)
        if wanted is None:
            raise ValueError("persisted experiment contains evidence outside attested benchmark")
        if float(persisted.value) != float(wanted.value):
            raise ValueError("persisted experiment evidence conflicts with attested benchmark")
    return observed


def _validate_terminal_snapshot(
    snapshot: ExperimentSnapshot,
    *,
    definition: ExperimentDefinition,
    expected_observations: tuple[MetricObservation, ...],
    expected_terminal: ExperimentSnapshot,
) -> ExperimentSnapshot:
    _validate_definition(snapshot, definition)
    _validate_definition(expected_terminal, definition)
    if snapshot.status not in {ExperimentStatus.COMPLETED, ExperimentStatus.PROMOTED}:
        raise ValueError("attested comparison did not reach a terminal experiment state")
    expected = _observation_map(expected_observations)
    observed = _validate_persisted_observations(snapshot, expected=expected)
    if set(observed) != set(expected):
        raise ValueError("terminal experiment observation coverage is inconsistent")
    if (
        snapshot.status is not expected_terminal.status
        or snapshot.selected_candidate_id != expected_terminal.selected_candidate_id
        or snapshot.previous_champion_id != expected_terminal.previous_champion_id
    ):
        raise ValueError(
            "terminal experiment conflicts with canonical Experiment Engine decision"
        )
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
    _validate_recoverable_snapshot(snapshot)
    if snapshot.status is ExperimentStatus.DRAFT:
        try:
            snapshot = engine.start(experiment_id)
        except ValueError:
            snapshot = repository.get(experiment_id)
            _validate_definition(snapshot, definition)
        _validate_recoverable_snapshot(snapshot)
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
    initial = repository.get(experiment_id)
    _validate_definition(initial, definition)
    _validate_recoverable_snapshot(initial)
    if initial.status is ExperimentStatus.RUNNING:
        _validate_persisted_observations(initial, expected=expected)
    for item in observations:
        key = _observation_key(item)
        snapshot = repository.get(experiment_id)
        _validate_definition(snapshot, definition)
        _validate_recoverable_snapshot(snapshot)
        if snapshot.status in {ExperimentStatus.COMPLETED, ExperimentStatus.PROMOTED}:
            return snapshot
        if snapshot.status is not ExperimentStatus.RUNNING:
            raise ValueError("experiment is not running while comparison evidence is incomplete")
        existing = _validate_persisted_observations(snapshot, expected=expected)
        if key in existing:
            continue
        try:
            engine.record(experiment_id, item)
        except ValueError:
            current = repository.get(experiment_id)
            _validate_definition(current, definition)
            _validate_recoverable_snapshot(current)
            if current.status in {ExperimentStatus.COMPLETED, ExperimentStatus.PROMOTED}:
                return current
            if current.status is not ExperimentStatus.RUNNING:
                raise
            persisted = _validate_persisted_observations(
                current,
                expected=expected,
            ).get(key)
            if persisted is not None:
                continue
            raise
    return repository.get(experiment_id)


def _finish_experiment(
    *,
    engine: ExperimentEngine,
    repository: ExperimentRepository,
    definition: ExperimentDefinition,
    expected_observations: tuple[MetricObservation, ...],
    expected_terminal: ExperimentSnapshot,
) -> ExperimentSnapshot:
    experiment_id = definition.experiment_id
    snapshot = repository.get(experiment_id)
    _validate_definition(snapshot, definition)
    if snapshot.status in {ExperimentStatus.COMPLETED, ExperimentStatus.PROMOTED}:
        return _validate_terminal_snapshot(
            snapshot,
            definition=definition,
            expected_observations=expected_observations,
            expected_terminal=expected_terminal,
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
        expected_terminal=expected_terminal,
    )


@dataclass(frozen=True, slots=True, init=False)
class AttestedTrainingComparisonResult:
    experiment_snapshot: ExperimentSnapshot
    champion_benchmark: AttestedChampionBenchmarkResult
    challenger_benchmark: AttestedChallengerBenchmarkResult
    definition_sha256: str
    observations_sha256: str

    def __init_subclass__(cls, **_: object) -> None:
        raise TypeError("AttestedTrainingComparisonResult cannot be subclassed")

    def _canonical_authorities(
        self,
    ) -> tuple[
        AttestedChampionBenchmarkResult,
        AttestedChallengerBenchmarkResult,
        TrainingEvaluationBinding,
    ]:
        if type(self.champion_benchmark) is not AttestedChampionBenchmarkResult:
            raise TypeError(
                "champion_benchmark must be an exact AttestedChampionBenchmarkResult"
            )
        if type(self.challenger_benchmark) is not AttestedChallengerBenchmarkResult:
            raise TypeError(
                "challenger_benchmark must be an exact AttestedChallengerBenchmarkResult"
            )
        champion = self.champion_benchmark.revalidated()
        challenger = self.challenger_benchmark.revalidated()
        if type(challenger.binding) is not TrainingEvaluationBinding:
            raise TypeError("challenger benchmark must carry exact training authority")
        training = challenger.binding.revalidated()
        champion_binding = champion.binding.revalidated()
        if (
            champion_binding.training_binding_sha256 != training.binding_sha256
            or champion_binding.job_id != training.job_id
            or champion_binding.candidate_id != training.base_candidate_id
            or champion_binding.provider_id != training.base_provider_id
            or champion_binding.model_id != training.base_model_id
            or champion_binding.artifact_sha256 != training.base_sha256
            or champion_binding.artifact_size_bytes != training.base_size_bytes
            or champion_binding.frozen_package_sha256
            != training.frozen_package_sha256
            or champion_binding.evaluation_set_sha256
            != training.evaluation_set_sha256
            or champion_binding.descriptor_digest != training.base_descriptor_digest
            or champion_binding.descriptor_registry_key
            != training.base_descriptor_registry_key
        ):
            raise ValueError(
                "champion benchmark does not match challenger training authority"
            )
        if (
            champion.report.evaluation_set_sha256
            != challenger.report.evaluation_set_sha256
            or champion.report.execution_config_sha256
            != challenger.report.execution_config_sha256
        ):
            raise ValueError("old/new benchmark authority changed")
        _validate_shared_attestor(champion, challenger)
        return champion, challenger, training

    def _validate(self) -> None:
        if type(self.experiment_snapshot) is not ExperimentSnapshot:
            raise TypeError("experiment_snapshot must be an exact ExperimentSnapshot")
        champion, challenger, _ = self._canonical_authorities()
        for value, name in (
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
        definition = self.experiment_snapshot.definition
        if (
            definition.champion.candidate_id != champion.report.candidate.candidate_id
            or len(definition.challengers) != 1
            or definition.challengers[0].candidate_id
            != challenger.report.candidate.candidate_id
        ):
            raise ValueError("experiment candidate authority changed")
        expected_terminal = _run_canonical_terminal(
            definition=self.experiment_snapshot.definition,
            observations=self.experiment_snapshot.observations,
        )
        _validate_terminal_snapshot(
            self.experiment_snapshot,
            definition=self.experiment_snapshot.definition,
            expected_observations=self.experiment_snapshot.observations,
            expected_terminal=expected_terminal,
        )

    def revalidated(self) -> AttestedTrainingComparisonResult:
        if type(self) is not AttestedTrainingComparisonResult:
            raise TypeError("result must be an exact AttestedTrainingComparisonResult")
        try:
            self._validate()
            champion, challenger, _ = self._canonical_authorities()
            return _build_result(
                experiment_snapshot=self.experiment_snapshot,
                champion_benchmark=champion,
                challenger_benchmark=challenger,
            )
        except AttributeError as exc:
            raise ValueError("attested comparison result fields are incomplete") from exc

    @property
    def training_binding_sha256(self) -> str:
        _, _, training = self._canonical_authorities()
        return training.binding_sha256

    @property
    def champion_binding_sha256(self) -> str:
        champion, _, _ = self._canonical_authorities()
        return champion.binding.binding_sha256

    @property
    def champion_benchmark_sha256(self) -> str:
        champion, _, _ = self._canonical_authorities()
        return champion.evidence_sha256

    @property
    def challenger_benchmark_sha256(self) -> str:
        _, challenger, _ = self._canonical_authorities()
        return challenger.evidence_sha256

    @property
    def champion_provider_manifest_sha256(self) -> str | None:
        champion, _, _ = self._canonical_authorities()
        return champion.provider_manifest_sha256

    @property
    def challenger_provider_manifest_sha256(self) -> str | None:
        _, challenger, _ = self._canonical_authorities()
        return challenger.provider_manifest_sha256

    @property
    def attestor_id(self) -> str:
        champion, _, _ = self._canonical_authorities()
        return champion.attestor_id

    @property
    def attestor_sha256(self) -> str:
        champion, _, _ = self._canonical_authorities()
        return champion.attestor_sha256

    def evidence_payload(self) -> dict[str, object]:
        result = self.revalidated()
        snapshot = result.experiment_snapshot
        payload: dict[str, object] = {
            "schema": "nika-attested-training-comparison-v1",
            "experiment_id": snapshot.definition.experiment_id,
            "experiment_status": snapshot.status.value,
            "selected_candidate_id": snapshot.selected_candidate_id,
            "previous_champion_id": snapshot.previous_champion_id,
            "training_binding_sha256": result.training_binding_sha256,
            "champion_binding_sha256": result.champion_binding_sha256,
            "champion_benchmark_sha256": result.champion_benchmark_sha256,
            "challenger_benchmark_sha256": result.challenger_benchmark_sha256,
            "attestor_id": result.attestor_id,
            "attestor_sha256": result.attestor_sha256,
            "definition_sha256": result.definition_sha256,
            "observations_sha256": result.observations_sha256,
            "observation_count": len(snapshot.observations),
        }
        if result.champion_provider_manifest_sha256 is not None:
            payload["champion_provider_manifest_sha256"] = (
                result.champion_provider_manifest_sha256
            )
        if result.challenger_provider_manifest_sha256 is not None:
            payload["challenger_provider_manifest_sha256"] = (
                result.challenger_provider_manifest_sha256
            )
        return payload

    @property
    def evidence_sha256(self) -> str:
        return attested_training_comparison_evidence_sha256(
            self.evidence_payload()
        )


_COMPARISON_EVIDENCE_REQUIRED_KEYS = frozenset(
    {
        "schema",
        "experiment_id",
        "experiment_status",
        "selected_candidate_id",
        "previous_champion_id",
        "training_binding_sha256",
        "champion_binding_sha256",
        "champion_benchmark_sha256",
        "challenger_benchmark_sha256",
        "attestor_id",
        "attestor_sha256",
        "definition_sha256",
        "observations_sha256",
        "observation_count",
    }
)
_COMPARISON_EVIDENCE_OPTIONAL_KEYS = frozenset(
    {
        "champion_provider_manifest_sha256",
        "challenger_provider_manifest_sha256",
    }
)


def attested_training_comparison_evidence_sha256(value: object) -> str:
    """Hash one strict canonical attested-comparison evidence payload."""

    if type(value) is not dict:
        raise TypeError("comparison evidence payload must be an exact object")
    keys = frozenset(value)
    if (
        not _COMPARISON_EVIDENCE_REQUIRED_KEYS.issubset(keys)
        or not keys.issubset(
            _COMPARISON_EVIDENCE_REQUIRED_KEYS
            | _COMPARISON_EVIDENCE_OPTIONAL_KEYS
        )
        or value.get("schema") != "nika-attested-training-comparison-v1"
    ):
        raise ValueError("comparison evidence payload does not match the strict schema")
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _build_result(
    *,
    experiment_snapshot: ExperimentSnapshot,
    champion_benchmark: AttestedChampionBenchmarkResult,
    challenger_benchmark: AttestedChallengerBenchmarkResult,
) -> AttestedTrainingComparisonResult:
    result = object.__new__(AttestedTrainingComparisonResult)
    object.__setattr__(result, "experiment_snapshot", experiment_snapshot)
    object.__setattr__(result, "champion_benchmark", champion_benchmark)
    object.__setattr__(result, "challenger_benchmark", challenger_benchmark)
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
    _validate_shared_attestor(champion, challenger)
    try:
        validate_evaluation_set(evaluation_set)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("evaluation set must be canonical") from exc
    if type(execution_config) is not BenchmarkExecutionConfig:
        raise TypeError("execution_config must be an exact BenchmarkExecutionConfig")
    BenchmarkExecutionConfig.__post_init__(execution_config)
    if type(challenger.binding) is not TrainingEvaluationBinding:
        raise TypeError("challenger benchmark must carry exact training authority")
    training_binding = challenger.binding.revalidated()
    champion_binding = champion.binding.revalidated()
    if champion_binding.training_binding_sha256 != training_binding.binding_sha256:
        raise ValueError("champion and challenger do not share one training authority")
    if (
        champion_binding.job_id != training_binding.job_id
        or champion_binding.candidate_id != training_binding.base_candidate_id
        or champion_binding.provider_id != training_binding.base_provider_id
        or champion_binding.model_id != training_binding.base_model_id
        or champion_binding.artifact_sha256 != training_binding.base_sha256
        or champion_binding.artifact_size_bytes != training_binding.base_size_bytes
        or champion_binding.frozen_package_sha256
        != training_binding.frozen_package_sha256
        or champion_binding.evaluation_set_sha256
        != training_binding.evaluation_set_sha256
        or champion_binding.descriptor_digest
        != training_binding.base_descriptor_digest
        or champion_binding.descriptor_registry_key
        != training_binding.base_descriptor_registry_key
        or champion.report.candidate.candidate_id
        != training_binding.base_candidate_id
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
    expected_terminal = _run_canonical_terminal(
        definition=definition,
        observations=observations,
    )
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
            expected_terminal=expected_terminal,
        )
    else:
        snapshot = _validate_terminal_snapshot(
            snapshot,
            definition=definition,
            expected_observations=observations,
            expected_terminal=expected_terminal,
        )
    return _build_result(
        experiment_snapshot=snapshot,
        champion_benchmark=champion,
        challenger_benchmark=challenger,
    )


__all__ = [
    "AttestedTrainingComparisonResult",
    "run_attested_old_vs_new_comparison",
]
