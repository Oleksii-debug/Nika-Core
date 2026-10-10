from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.experiments import (
    ArtifactKind,
    DatasetSplit,
    ExperimentDefinition,
    ExperimentEngine,
    ExperimentStatus,
    MetricObservation,
    PromotionPolicy,
    ReplayCase,
    SQLiteExperimentRepository,
    StrategyRef,
)


def _strategy(candidate_id: str) -> StrategyRef:
    return StrategyRef(
        candidate_id=candidate_id,
        version="1",
        artifact_kind=ArtifactKind.PROMPT,
        artifact_ref=f"prompt://{candidate_id}/1",
        permission_fingerprint="perm-v1",
    )


def _definition() -> ExperimentDefinition:
    cutoff = datetime(2026, 8, 1, tzinfo=UTC)
    return ExperimentDefinition(
        experiment_id="aud03-observation-type",
        champion=_strategy("champion"),
        challengers=(_strategy("challenger"),),
        replays=(
            ReplayCase(
                replay_id="r1",
                dataset_ref="dataset://evaluation",
                dataset_version="v1",
                split=DatasetSplit.EVALUATION,
                dataset_fingerprint="sha256:eval-v1",
                data_end_at=cutoff,
            ),
        ),
        policy=PromotionPolicy(
            primary_metric="quality",
            minimum_improvement=0.05,
        ),
        evaluation_cutoff=cutoff,
    )


def _create_running_experiment(path: Path) -> SQLiteStore:
    store = SQLiteStore(path)
    store.initialize()
    repository = SQLiteExperimentRepository(store)
    engine = ExperimentEngine(repository)
    engine.create(_definition())
    engine.start("aud03-observation-type")
    engine.record(
        "aud03-observation-type",
        MetricObservation("champion", "r1", "quality", 0.70),
    )
    engine.record(
        "aud03-observation-type",
        MetricObservation("challenger", "r1", "quality", 0.80),
    )
    return store


def test_restart_rejects_blob_metric_value_before_contract_coercion(tmp_path: Path) -> None:
    path = tmp_path / "nika.db"
    store = _create_running_experiment(path)

    with store.connection() as conn:
        conn.execute(
            """UPDATE experiment_observations
            SET value=?
            WHERE experiment_id=? AND candidate_id=? AND replay_id=? AND metric=?""",
            (
                sqlite3.Binary(b"0.80"),
                "aud03-observation-type",
                "challenger",
                "r1",
                "quality",
            ),
        )
        stored_type = conn.execute(
            """SELECT typeof(value) AS storage_type
            FROM experiment_observations
            WHERE experiment_id=? AND candidate_id=?""",
            ("aud03-observation-type", "challenger"),
        ).fetchone()["storage_type"]
    assert stored_type == "blob"

    restarted_store = SQLiteStore(path)
    restarted_store.initialize()
    restarted = SQLiteExperimentRepository(restarted_store)

    with pytest.raises((TypeError, ValueError, RuntimeError)):
        restarted.get("aud03-observation-type")


def test_normal_real_metric_values_remain_valid_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "nika.db"
    store = _create_running_experiment(path)

    with store.connection() as conn:
        storage_types = tuple(
            row["storage_type"]
            for row in conn.execute(
                """SELECT typeof(value) AS storage_type
                FROM experiment_observations
                WHERE experiment_id=?
                ORDER BY candidate_id""",
                ("aud03-observation-type",),
            ).fetchall()
        )
    assert storage_types == ("real", "real")

    restarted_store = SQLiteStore(path)
    restarted_store.initialize()
    restarted = SQLiteExperimentRepository(restarted_store)
    snapshot = restarted.get("aud03-observation-type")

    assert snapshot.status is ExperimentStatus.RUNNING
    assert tuple(item.value for item in snapshot.observations) == (0.70, 0.80)
