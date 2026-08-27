from __future__ import annotations

from dataclasses import replace

from nika_core.experiments.contracts import (
    ExperimentDefinition,
    ExperimentSnapshot,
    ExperimentStatus,
    MetricObservation,
)
from nika_core.experiments.decision import decide_terminal
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
        decision = decide_terminal(snapshot)
        updated = replace(
            snapshot,
            status=decision.status,
            selected_candidate_id=decision.selected_candidate_id,
            previous_champion_id=decision.previous_champion_id,
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
