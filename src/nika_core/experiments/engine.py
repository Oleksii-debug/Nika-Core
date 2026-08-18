from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from statistics import fmean

from nika_core.experiments.contracts import (
    ExperimentDefinition,
    ExperimentSnapshot,
    ExperimentStatus,
    MetricObservation,
    MetricRule,
)
from nika_core.experiments.repository import ExperimentRepository


class ExperimentEngine:
    """Controlled replay-based promotion engine with no source mutation capability."""

    def __init__(self, repository: ExperimentRepository) -> None:
        self._repository = repository

    def create(self, definition: ExperimentDefinition) -> ExperimentSnapshot:
        snapshot = ExperimentSnapshot(definition=definition)
        self._repository.create(snapshot)
        return snapshot

    def start(self, experiment_id: str) -> ExperimentSnapshot:
        snapshot = self._repository.get(experiment_id)
        if snapshot.status is not ExperimentStatus.DRAFT:
            raise ValueError("only draft experiments can start")
        updated = replace(snapshot, status=ExperimentStatus.RUNNING)
        self._repository.save(updated)
        return updated

    def record(self, experiment_id: str, observation: MetricObservation) -> ExperimentSnapshot:
        snapshot = self._repository.get(experiment_id)
        if snapshot.status is not ExperimentStatus.RUNNING:
            raise ValueError("observations require a running experiment")
        candidate_ids = {
            snapshot.definition.champion.candidate_id,
            *(item.candidate_id for item in snapshot.definition.challengers),
        }
        replay_ids = {item.replay_id for item in snapshot.definition.replays}
        allowed_metrics = {
            snapshot.definition.policy.primary_metric,
            *(rule.metric for rule in snapshot.definition.policy.guardrails),
        }
        if observation.candidate_id not in candidate_ids:
            raise ValueError("observation references an unknown candidate")
        if observation.replay_id not in replay_ids:
            raise ValueError("observation references an unknown replay")
        if observation.metric not in allowed_metrics:
            raise ValueError("observation references an undeclared metric")
        key = (observation.candidate_id, observation.replay_id, observation.metric)
        existing = {
            (item.candidate_id, item.replay_id, item.metric) for item in snapshot.observations
        }
        if key in existing:
            raise ValueError("duplicate candidate/replay/metric observation")
        updated = replace(snapshot, observations=(*snapshot.observations, observation))
        self._repository.save(updated)
        return updated

    def complete(self, experiment_id: str) -> ExperimentSnapshot:
        snapshot = self._repository.get(experiment_id)
        if snapshot.status is not ExperimentStatus.RUNNING:
            raise ValueError("only running experiments can complete")
        self._validate_coverage(snapshot)
        champion_id = snapshot.definition.champion.candidate_id
        primary = snapshot.definition.policy.primary_metric
        champion_score = self._mean(snapshot, champion_id, primary)
        eligible: list[tuple[str, float]] = []
        for challenger in snapshot.definition.challengers:
            candidate_id = challenger.candidate_id
            score = self._mean(snapshot, candidate_id, primary)
            improvement = score - champion_score
            if improvement < snapshot.definition.policy.minimum_improvement:
                continue
            if self._guardrails_pass(snapshot, champion_id, candidate_id):
                eligible.append((candidate_id, score))
        selected = champion_id
        status = ExperimentStatus.COMPLETED
        if eligible:
            selected = max(eligible, key=lambda item: (item[1], item[0]))[0]
            status = ExperimentStatus.PROMOTED
        updated = replace(
            snapshot,
            status=status,
            selected_candidate_id=selected,
            previous_champion_id=champion_id,
        )
        self._repository.save(updated)
        return updated

    def rollback(self, experiment_id: str) -> ExperimentSnapshot:
        snapshot = self._repository.get(experiment_id)
        if snapshot.status is not ExperimentStatus.PROMOTED:
            raise ValueError("rollback requires a promoted experiment")
        if snapshot.previous_champion_id is None:
            raise RuntimeError("promotion has no previous champion evidence")
        updated = replace(
            snapshot,
            status=ExperimentStatus.ROLLED_BACK,
            selected_candidate_id=snapshot.previous_champion_id,
        )
        self._repository.save(updated)
        return updated

    def _validate_coverage(self, snapshot: ExperimentSnapshot) -> None:
        required_replays = {item.replay_id for item in snapshot.definition.replays}
        candidate_ids = (
            snapshot.definition.champion.candidate_id,
            *(item.candidate_id for item in snapshot.definition.challengers),
        )
        metrics = (
            snapshot.definition.policy.primary_metric,
            *(rule.metric for rule in snapshot.definition.policy.guardrails),
        )
        for candidate_id in candidate_ids:
            for metric in metrics:
                covered = {
                    item.replay_id
                    for item in snapshot.observations
                    if item.candidate_id == candidate_id and item.metric == metric
                }
                if covered != required_replays:
                    raise ValueError(
                        f"incomplete replay coverage for {candidate_id}/{metric}: "
                        f"expected {len(required_replays)}, got {len(covered)}"
                    )

    def _guardrails_pass(
        self, snapshot: ExperimentSnapshot, champion_id: str, candidate_id: str
    ) -> bool:
        return all(
            self._guardrail_pass(snapshot, champion_id, candidate_id, rule)
            for rule in snapshot.definition.policy.guardrails
        )

    def _guardrail_pass(
        self,
        snapshot: ExperimentSnapshot,
        champion_id: str,
        candidate_id: str,
        rule: MetricRule,
    ) -> bool:
        champion = self._mean(snapshot, champion_id, rule.metric)
        challenger = self._mean(snapshot, candidate_id, rule.metric)
        regression = champion - challenger if rule.higher_is_better else challenger - champion
        return regression <= rule.max_regression

    @staticmethod
    def _mean(snapshot: ExperimentSnapshot, candidate_id: str, metric: str) -> float:
        values: defaultdict[str, list[float]] = defaultdict(list)
        for item in snapshot.observations:
            if item.metric == metric:
                values[item.candidate_id].append(float(item.value))
        if candidate_id not in values:
            raise ValueError(f"missing metric {metric} for candidate {candidate_id}")
        return fmean(values[candidate_id])
