from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from statistics import fmean


class ExperimentStatus(StrEnum):
    DRAFT = "draft"
    RUNNING = "running"
    COMPLETED = "completed"
    PROMOTED = "promoted"
    ROLLED_BACK = "rolled_back"


@dataclass(frozen=True, slots=True)
class Candidate:
    candidate_id: str
    version: str
    artifact_ref: str


@dataclass(frozen=True, slots=True)
class Evaluation:
    candidate_id: str
    metrics: Mapping[str, float]
    replay_id: str


@dataclass(slots=True)
class Experiment:
    """Controlled champion/challenger experiment without source-code mutation capability."""

    experiment_id: str
    champion: Candidate
    challengers: tuple[Candidate, ...]
    primary_metric: str
    minimum_improvement: float = 0.0
    status: ExperimentStatus = ExperimentStatus.DRAFT
    evaluations: list[Evaluation] = field(default_factory=list)
    promoted_candidate_id: str | None = None

    def start(self) -> None:
        if self.status is not ExperimentStatus.DRAFT:
            raise ValueError("only draft experiments can start")
        if not self.challengers:
            raise ValueError("at least one challenger is required")
        self.status = ExperimentStatus.RUNNING

    def record(self, evaluation: Evaluation) -> None:
        if self.status is not ExperimentStatus.RUNNING:
            raise ValueError("experiment is not running")
        valid = {self.champion.candidate_id, *(c.candidate_id for c in self.challengers)}
        if evaluation.candidate_id not in valid:
            raise ValueError("evaluation references an unknown candidate")
        if self.primary_metric not in evaluation.metrics:
            raise ValueError(f"missing primary metric: {self.primary_metric}")
        self.evaluations.append(evaluation)

    def scores(self) -> dict[str, float]:
        grouped: dict[str, list[float]] = {}
        for evaluation in self.evaluations:
            grouped.setdefault(evaluation.candidate_id, []).append(
                float(evaluation.metrics[self.primary_metric])
            )
        return {candidate_id: fmean(values) for candidate_id, values in grouped.items()}

    def complete(self) -> str:
        if self.status is not ExperimentStatus.RUNNING:
            raise ValueError("experiment is not running")
        scores = self.scores()
        champion_score = scores.get(self.champion.candidate_id)
        if champion_score is None:
            raise ValueError("champion must have evaluation data")
        missing = [c.candidate_id for c in self.challengers if c.candidate_id not in scores]
        if missing:
            raise ValueError(f"challengers missing evaluation data: {', '.join(missing)}")
        best = max(self.challengers, key=lambda candidate: scores[candidate.candidate_id])
        self.status = ExperimentStatus.COMPLETED
        if scores[best.candidate_id] - champion_score >= self.minimum_improvement:
            self.promoted_candidate_id = best.candidate_id
            self.status = ExperimentStatus.PROMOTED
            return best.candidate_id
        self.promoted_candidate_id = self.champion.candidate_id
        return self.champion.candidate_id

    def rollback(self) -> str:
        if self.status not in {ExperimentStatus.PROMOTED, ExperimentStatus.COMPLETED}:
            raise ValueError("only completed experiments can roll back")
        self.promoted_candidate_id = self.champion.candidate_id
        self.status = ExperimentStatus.ROLLED_BACK
        return self.champion.candidate_id


def replay_coverage(evaluations: Iterable[Evaluation]) -> tuple[str, ...]:
    return tuple(sorted({evaluation.replay_id for evaluation in evaluations}))
