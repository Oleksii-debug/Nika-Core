from __future__ import annotations

from datetime import datetime

from nika_core.model_engineering.contracts import (
    BenchmarkObservation,
    BenchmarkRecommendation,
    BenchmarkSuite,
)
from nika_core.model_engineering.repository import SQLiteModelEngineeringRepository
from nika_core.model_engineering.scoring import rank_benchmark_candidates


class ModelEngineeringLab:
    """Durable benchmark evidence and review-only ranking facade.

    This service deliberately has no ModelGateway mutation or provider-promotion method.
    It records benchmark evidence and emits a recommendation that cannot authorize a
    production model change.
    """

    def __init__(self, repository: SQLiteModelEngineeringRepository) -> None:
        self._repository = repository

    def initialize(self) -> None:
        self._repository.initialize()

    def register_suite(self, suite: BenchmarkSuite) -> None:
        self._repository.save_suite(suite)

    def record_observation(self, observation: BenchmarkObservation) -> None:
        suite = self._repository.get_suite(observation.suite_key)
        if suite is None:
            raise KeyError(f"unknown benchmark suite: {observation.suite_key}")
        if {metric.name for metric in observation.metrics} != {
            metric.name for metric in suite.metrics
        }:
            raise ValueError("observation metric set does not match registered benchmark suite")
        if observation.case_id not in suite.required_case_ids:
            raise ValueError("observation case_id is not part of the registered benchmark suite")
        self._repository.save_observation(observation)

    def recommend(
        self,
        run_id: str,
        suite_key: str,
        *,
        created_at: datetime | None = None,
    ) -> BenchmarkRecommendation:
        suite = self._repository.get_suite(suite_key)
        if suite is None:
            raise KeyError(f"unknown benchmark suite: {suite_key}")
        existing = self._repository.get_recommendation(run_id, suite_key)
        observations = self._repository.list_observations(run_id, suite_key)
        if existing is not None:
            recomputed = rank_benchmark_candidates(
                suite,
                observations,
                created_at=existing.created_at,
            )
            if recomputed != existing:
                raise RuntimeError("persisted recommendation no longer matches benchmark evidence")
            return existing
        recommendation = rank_benchmark_candidates(
            suite,
            observations,
            created_at=created_at,
        )
        self._repository.save_recommendation(recommendation)
        return recommendation

    def get_recommendation(
        self, run_id: str, suite_key: str
    ) -> BenchmarkRecommendation | None:
        return self._repository.get_recommendation(run_id, suite_key)
