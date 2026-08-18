from __future__ import annotations

from copy import deepcopy
from typing import Protocol

from nika_core.experiments.contracts import ExperimentSnapshot


class ExperimentRepository(Protocol):
    def create(self, snapshot: ExperimentSnapshot) -> None: ...

    def get(self, experiment_id: str) -> ExperimentSnapshot: ...

    def save(self, snapshot: ExperimentSnapshot) -> None: ...


class InMemoryExperimentRepository:
    """Deterministic test/prototype adapter behind the M8 repository port."""

    def __init__(self) -> None:
        self._items: dict[str, ExperimentSnapshot] = {}

    def create(self, snapshot: ExperimentSnapshot) -> None:
        experiment_id = snapshot.definition.experiment_id
        if experiment_id in self._items:
            raise ValueError(f"experiment already exists: {experiment_id}")
        self._items[experiment_id] = deepcopy(snapshot)

    def get(self, experiment_id: str) -> ExperimentSnapshot:
        try:
            return deepcopy(self._items[experiment_id])
        except KeyError as exc:
            raise KeyError(f"unknown experiment: {experiment_id}") from exc

    def save(self, snapshot: ExperimentSnapshot) -> None:
        experiment_id = snapshot.definition.experiment_id
        if experiment_id not in self._items:
            raise KeyError(f"unknown experiment: {experiment_id}")
        self._items[experiment_id] = deepcopy(snapshot)
