from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from nika_core.data.schema import SCHEMA_VERSION
from nika_core.data.sqlite import SQLiteStore
from nika_core.experiments import (
    ArtifactKind,
    ExperimentDefinition,
    ExperimentEngine,
    ExperimentStatus,
    MetricObservation,
    MetricRule,
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
    return ExperimentDefinition(
        experiment_id="exp-durable",
        champion=_strategy("champion"),
        challengers=(_strategy("challenger-a"), _strategy("challenger-b")),
        replays=(
            ReplayCase("r1", "dataset://qa", "v1"),
            ReplayCase("r2", "dataset://qa", "v1"),
        ),
        policy=PromotionPolicy(
            primary_metric="quality",
            minimum_improvement=0.05,
            minimum_replays=2,
            guardrails=(MetricRule("safety", max_regression=0.02),),
        ),
    )


def _engine(path: Path) -> tuple[SQLiteStore, SQLiteExperimentRepository, ExperimentEngine]:
    store = SQLiteStore(path)
    store.initialize()
    repository = SQLiteExperimentRepository(store)
    return store, repository, ExperimentEngine(repository)


def _record_pair(
    engine: ExperimentEngine,
    candidate_id: str,
    quality: tuple[float, float],
    safety: tuple[float, float],
) -> None:
    for replay_id, quality_value, safety_value in zip(("r1", "r2"), quality, safety, strict=True):
        engine.record(
            "exp-durable",
            MetricObservation(candidate_id, replay_id, "quality", quality_value),
        )
        engine.record(
            "exp-durable",
            MetricObservation(candidate_id, replay_id, "safety", safety_value),
        )


def test_current_schema_durable_restart_promotion_and_rollback(tmp_path: Path) -> None:
    path = tmp_path / "nika.db"
    store, _, first = _engine(path)
    assert store.schema_version() == SCHEMA_VERSION
    first.create(_definition())
    first.start("exp-durable")
    _record_pair(first, "champion", (0.70, 0.70), (0.95, 0.95))

    _, second_repository, second = _engine(path)
    recovered = second_repository.get("exp-durable")
    assert recovered.status is ExperimentStatus.RUNNING
    assert len(recovered.observations) == 4
    _record_pair(second, "challenger-a", (0.82, 0.82), (0.95, 0.95))
    _record_pair(second, "challenger-b", (0.71, 0.71), (0.95, 0.95))
    promoted = second.complete("exp-durable")
    assert promoted.status is ExperimentStatus.PROMOTED
    assert promoted.selected_candidate_id == "challenger-a"

    _, _, third = _engine(path)
    rolled_back = third.rollback("exp-durable")
    assert rolled_back.status is ExperimentStatus.ROLLED_BACK
    assert rolled_back.selected_candidate_id == "champion"

    with store.connection() as conn:
        events = conn.execute(
            "SELECT new_status FROM experiment_events WHERE experiment_id = ? ORDER BY event_id",
            ("exp-durable",),
        ).fetchall()
    assert [row["new_status"] for row in events] == [
        "draft",
        "running",
        "promoted",
        "rolled_back",
    ]


def test_transition_and_event_write_roll_back_together_on_fault(tmp_path: Path) -> None:
    path = tmp_path / "nika.db"
    store, _, engine = _engine(path)
    engine.create(_definition())
    with store.connection() as conn:
        conn.execute(
            """CREATE TRIGGER fail_running_event
            BEFORE INSERT ON experiment_events
            WHEN NEW.new_status = 'running'
            BEGIN
                SELECT RAISE(ABORT, 'injected transition failure');
            END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected transition failure"):
        engine.start("exp-durable")

    with store.connection() as conn:
        conn.execute("DROP TRIGGER fail_running_event")
    recreated = SQLiteExperimentRepository(SQLiteStore(path))
    snapshot = recreated.get("exp-durable")
    assert snapshot.status is ExperimentStatus.DRAFT
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT new_status FROM experiment_events WHERE experiment_id = ? ORDER BY event_id",
            ("exp-durable",),
        ).fetchall()
    assert [row["new_status"] for row in rows] == ["draft"]


def test_definition_and_recorded_evidence_are_immutable(tmp_path: Path) -> None:
    _, repository, engine = _engine(tmp_path / "nika.db")
    engine.create(_definition())
    running = engine.start("exp-durable")
    observation = MetricObservation("champion", "r1", "quality", 0.7)
    saved = engine.record("exp-durable", observation)

    with pytest.raises(ValueError, match="append-only"):
        repository.save(replace(saved, observations=()))
    changed_observation = replace(observation, value=0.9)
    with pytest.raises(ValueError, match="immutable"):
        repository.save(replace(saved, observations=(changed_observation,)))
    changed_definition = replace(
        running.definition,
        replays=(
            ReplayCase("r1", "dataset://other", "v2"),
            running.definition.replays[1],
        ),
    )
    with pytest.raises(ValueError, match="definition is immutable"):
        repository.save(replace(saved, definition=changed_definition))


def test_stale_writer_cannot_drop_concurrent_evidence(tmp_path: Path) -> None:
    _, repository, engine = _engine(tmp_path / "nika.db")
    engine.create(_definition())
    engine.start("exp-durable")
    stale_a = repository.get("exp-durable")
    stale_b = repository.get("exp-durable")
    first = MetricObservation("champion", "r1", "quality", 0.7)
    second = MetricObservation("champion", "r2", "quality", 0.8)
    repository.save(replace(stale_a, observations=(first,)))
    with pytest.raises(ValueError, match="append-only"):
        repository.save(replace(stale_b, observations=(second,)))
    current = repository.get("exp-durable")
    assert current.observations == (first,)


def test_repository_rejects_illegal_state_jump(tmp_path: Path) -> None:
    _, repository, engine = _engine(tmp_path / "nika.db")
    draft = engine.create(_definition())
    with pytest.raises(ValueError, match="invalid experiment transition"):
        repository.save(
            replace(
                draft,
                status=ExperimentStatus.PROMOTED,
                selected_candidate_id="challenger-a",
                previous_champion_id="champion",
            )
        )
