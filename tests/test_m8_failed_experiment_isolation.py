from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.experiments import (
    ArtifactKind,
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


def _definition(experiment_id: str) -> ExperimentDefinition:
    return ExperimentDefinition(
        experiment_id=experiment_id,
        champion=_strategy("champion"),
        challengers=(_strategy("challenger"),),
        replays=(
            ReplayCase("r1", "dataset://qa", "v1"),
            ReplayCase("r2", "dataset://qa", "v1"),
        ),
        policy=PromotionPolicy(
            primary_metric="quality",
            minimum_improvement=0.05,
            minimum_replays=2,
        ),
    )


def _repository(path: Path) -> tuple[SQLiteStore, SQLiteExperimentRepository]:
    store = SQLiteStore(path)
    store.initialize()
    return store, SQLiteExperimentRepository(store)


def _record_complete_evidence(engine: ExperimentEngine, experiment_id: str) -> None:
    for candidate_id, score in (("champion", 0.70), ("challenger", 0.82)):
        for replay_id in ("r1", "r2"):
            engine.record(
                experiment_id,
                MetricObservation(candidate_id, replay_id, "quality", score),
            )


def _event_statuses(store: SQLiteStore, experiment_id: str) -> list[str]:
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT new_status FROM experiment_events WHERE experiment_id = ? "
            "ORDER BY event_id",
            (experiment_id,),
        ).fetchall()
    return [str(row["new_status"]) for row in rows]


def test_terminal_promotion_fault_is_atomic_and_restart_safe(tmp_path: Path) -> None:
    path = tmp_path / "nika.db"
    store, repository = _repository(path)
    engine = ExperimentEngine(repository)

    engine.create(_definition("exp-accepted"))
    engine.start("exp-accepted")
    _record_complete_evidence(engine, "exp-accepted")
    accepted_before = engine.complete("exp-accepted")
    assert accepted_before.status is ExperimentStatus.PROMOTED
    assert accepted_before.selected_candidate_id == "challenger"

    engine.create(_definition("exp-failing"))
    engine.start("exp-failing")
    _record_complete_evidence(engine, "exp-failing")
    failing_before = repository.get("exp-failing")

    with store.connection() as conn:
        conn.execute(
            """CREATE TRIGGER fail_promoted_event
            BEFORE INSERT ON experiment_events
            WHEN NEW.experiment_id = 'exp-failing'
                 AND NEW.new_status = 'promoted'
            BEGIN
                SELECT RAISE(ABORT, 'injected terminal failure');
            END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected terminal failure"):
        engine.complete("exp-failing")

    with store.connection() as conn:
        conn.execute("DROP TRIGGER fail_promoted_event")

    restarted_store, restarted_repository = _repository(path)
    failed_after_restart = restarted_repository.get("exp-failing")
    accepted_after_restart = restarted_repository.get("exp-accepted")

    assert failed_after_restart == failing_before
    assert failed_after_restart.status is ExperimentStatus.RUNNING
    assert failed_after_restart.selected_candidate_id is None
    assert failed_after_restart.previous_champion_id is None
    assert len(failed_after_restart.observations) == 4

    assert accepted_after_restart == accepted_before
    assert accepted_after_restart.status is ExperimentStatus.PROMOTED
    assert accepted_after_restart.selected_candidate_id == "challenger"
    assert len(accepted_after_restart.observations) == 4

    assert _event_statuses(restarted_store, "exp-failing") == ["draft", "running"]
    assert _event_statuses(restarted_store, "exp-accepted") == [
        "draft",
        "running",
        "promoted",
    ]


def test_failed_completion_validation_preserves_durable_state_on_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "nika.db"
    _, repository = _repository(path)
    engine = ExperimentEngine(repository)
    engine.create(_definition("exp-failing"))
    engine.start("exp-failing")
    engine.record(
        "exp-failing",
        MetricObservation("champion", "r1", "quality", 0.70),
    )
    before_failure = repository.get("exp-failing")

    with pytest.raises(ValueError, match="incomplete replay coverage"):
        engine.complete("exp-failing")

    restarted_store, restarted_repository = _repository(path)
    after_restart = restarted_repository.get("exp-failing")
    assert after_restart == before_failure
    assert after_restart.status is ExperimentStatus.RUNNING
    assert after_restart.selected_candidate_id is None
    assert after_restart.previous_champion_id is None
    assert _event_statuses(restarted_store, "exp-failing") == ["draft", "running"]
