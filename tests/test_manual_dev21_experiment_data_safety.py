from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.experiments import (
    ArtifactKind,
    DatasetSplit,
    ExperimentDefinition,
    ExperimentEngine,
    ExperimentStatus,
    InMemoryExperimentRepository,
    MetricObservation,
    PromotionPolicy,
    ReplayCase,
    SQLiteExperimentRepository,
    StrategyRef,
)


def _strategy(
    candidate_id: str,
    *,
    training: tuple[str, ...] = (),
) -> StrategyRef:
    return StrategyRef(
        candidate_id=candidate_id,
        version="1",
        artifact_kind=ArtifactKind.STRATEGY,
        artifact_ref=f"strategy://{candidate_id}/1",
        permission_fingerprint="perm-v1",
        training_dataset_fingerprints=training,
    )


def _definition(
    *,
    training: tuple[str, ...] = (),
    replay_fingerprint: str | None = "sha256:eval-v1",
    replay_split: DatasetSplit = DatasetSplit.HELD_OUT,
    data_end_at: datetime | None = None,
    evaluation_cutoff: datetime | None = None,
) -> ExperimentDefinition:
    return ExperimentDefinition(
        experiment_id="exp-data-safety",
        champion=_strategy("champion", training=training),
        challengers=(_strategy("challenger", training=training),),
        replays=(
            ReplayCase(
                replay_id="r1",
                dataset_ref="dataset://evaluation",
                dataset_version="v1",
                split=replay_split,
                dataset_fingerprint=replay_fingerprint,
                data_end_at=data_end_at,
            ),
        ),
        policy=PromotionPolicy(primary_metric="quality"),
        evaluation_cutoff=evaluation_cutoff,
    )


def _legacy_definition_payload() -> dict[str, object]:
    return {
        "experiment_id": "legacy-exp",
        "champion": {
            "candidate_id": "champion",
            "version": "1",
            "artifact_kind": "prompt",
            "artifact_ref": "prompt://champion/1",
            "permission_fingerprint": "perm-v1",
        },
        "challengers": [
            {
                "candidate_id": "challenger",
                "version": "1",
                "artifact_kind": "prompt",
                "artifact_ref": "prompt://challenger/1",
                "permission_fingerprint": "perm-v1",
            }
        ],
        "replays": [
            {
                "replay_id": "r1",
                "dataset_ref": "dataset://legacy",
                "dataset_version": "v1",
            }
        ],
        "policy": {
            "primary_metric": "quality",
            "minimum_improvement": 0.0,
            "minimum_replays": 1,
            "guardrails": [],
            "primary_higher_is_better": True,
        },
    }


def _persist_definition(
    tmp_path: Path,
    definition: dict[str, object],
    *,
    experiment_id: str = "legacy-exp",
) -> SQLiteExperimentRepository:
    path = tmp_path / "nika.db"
    store = SQLiteStore(path)
    store.initialize()
    now = datetime.now(UTC).isoformat()
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO experiments(experiment_id, definition_json, status, "
            "selected_candidate_id, previous_champion_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                experiment_id,
                json.dumps(definition, sort_keys=True, separators=(",", ":")),
                "draft",
                None,
                None,
                now,
                now,
            ),
        )
    return SQLiteExperimentRepository(SQLiteStore(path))


def test_dataset_fingerprints_require_canonical_exact_identity() -> None:
    with pytest.raises(ValueError, match="canonical without whitespace"):
        _strategy("candidate", training=("sha256:train-v1 ",))
    with pytest.raises(ValueError, match="canonical without whitespace"):
        ReplayCase(
            "r1",
            "dataset://evaluation",
            "v1",
            dataset_fingerprint=" sha256:eval-v1",
        )


def test_replay_without_proven_split_defaults_to_evaluation_not_held_out() -> None:
    replay = ReplayCase("r1", "dataset://evaluation", "v1")
    assert replay.split is DatasetSplit.EVALUATION


def test_training_dataset_cannot_be_reused_as_promotion_replay() -> None:
    with pytest.raises(ValueError, match="overlaps candidate training data"):
        _definition(training=("sha256:eval-v1",))


def test_declared_training_provenance_requires_replay_fingerprint() -> None:
    with pytest.raises(ValueError, match="fingerprints are required"):
        _definition(training=("sha256:train-v1",), replay_fingerprint=None)


def test_training_split_is_never_valid_promotion_evidence() -> None:
    with pytest.raises(ValueError, match="training split"):
        _definition(replay_split=DatasetSplit.TRAINING)


def test_future_data_after_declared_evaluation_cutoff_fails_closed() -> None:
    cutoff = datetime(2026, 8, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="after the declared cutoff"):
        _definition(
            data_end_at=cutoff + timedelta(seconds=1),
            evaluation_cutoff=cutoff,
        )


def test_declared_cutoff_requires_temporal_provenance_for_every_replay() -> None:
    cutoff = datetime(2026, 8, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="data_end_at is required"):
        _definition(data_end_at=None, evaluation_cutoff=cutoff)


def test_timezone_naive_temporal_evidence_is_rejected() -> None:
    naive = datetime(2026, 8, 1, tzinfo=UTC).replace(tzinfo=None)
    with pytest.raises(ValueError, match="timezone-aware"):
        ReplayCase(
            "r1",
            "dataset://evaluation",
            "v1",
            data_end_at=naive,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        _definition(evaluation_cutoff=naive)


def test_causally_valid_held_out_data_can_drive_promotion() -> None:
    cutoff = datetime(2026, 8, 1, tzinfo=UTC)
    engine = ExperimentEngine(InMemoryExperimentRepository())
    engine.create(
        _definition(
            training=("sha256:train-v1",),
            replay_fingerprint="sha256:heldout-v1",
            data_end_at=cutoff,
            evaluation_cutoff=cutoff,
        )
    )
    engine.start("exp-data-safety")
    engine.record("exp-data-safety", MetricObservation("champion", "r1", "quality", 0.70))
    engine.record("exp-data-safety", MetricObservation("challenger", "r1", "quality", 0.80))
    result = engine.complete("exp-data-safety")
    assert result.status is ExperimentStatus.PROMOTED
    assert result.selected_candidate_id == "challenger"


def test_dataset_provenance_and_cutoff_survive_sqlite_restart(tmp_path: Path) -> None:
    cutoff = datetime(2026, 8, 1, 12, 30, tzinfo=UTC)
    path = tmp_path / "nika.db"
    store = SQLiteStore(path)
    store.initialize()
    first_repository = SQLiteExperimentRepository(store)
    first = ExperimentEngine(first_repository)
    definition = _definition(
        training=("sha256:train-v1",),
        replay_fingerprint="sha256:heldout-v1",
        data_end_at=cutoff - timedelta(hours=1),
        evaluation_cutoff=cutoff,
    )
    first.create(definition)
    first.start("exp-data-safety")
    first.record("exp-data-safety", MetricObservation("champion", "r1", "quality", 0.70))

    reloaded_store = SQLiteStore(path)
    reloaded_store.initialize()
    second_repository = SQLiteExperimentRepository(reloaded_store)
    recovered = second_repository.get("exp-data-safety")
    assert recovered.definition == definition
    assert recovered.definition.replays[0].split is DatasetSplit.HELD_OUT
    assert recovered.definition.replays[0].dataset_fingerprint == "sha256:heldout-v1"
    assert recovered.definition.evaluation_cutoff == cutoff

    second = ExperimentEngine(second_repository)
    second.record("exp-data-safety", MetricObservation("challenger", "r1", "quality", 0.80))
    promoted = second.complete("exp-data-safety")
    assert promoted.status is ExperimentStatus.PROMOTED


def test_legacy_persisted_definition_keeps_unknown_split_as_evaluation(tmp_path: Path) -> None:
    recovered = _persist_definition(tmp_path, _legacy_definition_payload()).get("legacy-exp")

    assert recovered.definition.replays[0].split is DatasetSplit.EVALUATION
    assert recovered.definition.replays[0].dataset_fingerprint is None
    assert recovered.definition.replays[0].data_end_at is None
    assert recovered.definition.evaluation_cutoff is None
    assert recovered.definition.champion.training_dataset_fingerprints == ()


def test_persisted_policy_direction_string_fails_closed(tmp_path: Path) -> None:
    definition = _legacy_definition_payload()
    policy = definition["policy"]
    assert isinstance(policy, dict)
    policy["primary_higher_is_better"] = "false"
    repository = _persist_definition(tmp_path, definition)

    with pytest.raises(TypeError, match="primary_higher_is_better"):
        repository.get("legacy-exp")


def test_persisted_numeric_training_fingerprint_fails_closed(tmp_path: Path) -> None:
    definition = _legacy_definition_payload()
    champion = definition["champion"]
    assert isinstance(champion, dict)
    champion["training_dataset_fingerprints"] = [123]
    repository = _persist_definition(tmp_path, definition)

    with pytest.raises(TypeError, match="contain only strings"):
        repository.get("legacy-exp")


def test_persisted_minimum_replays_string_fails_closed(tmp_path: Path) -> None:
    definition = _legacy_definition_payload()
    policy = definition["policy"]
    assert isinstance(policy, dict)
    policy["minimum_replays"] = "1"
    repository = _persist_definition(tmp_path, definition)

    with pytest.raises(TypeError, match="minimum_replays"):
        repository.get("legacy-exp")
