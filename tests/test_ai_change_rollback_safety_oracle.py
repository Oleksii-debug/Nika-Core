from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from nika_core.builder.repository import AgentDefinitionRepository
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
from nika_core.kernel.task_queue import TaskQueue
from nika_core.v01_model_settings import (
    ModelSelection,
    ModelSetupError,
    V01BoundModelRuntimeFactory,
    V01ModelSettings,
)


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "profile" / "nika.db")
    store.initialize()
    return store


def _local(*, revision: int, model: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "route_kind": "ollama",
        "provider_id": "ollama",
        "model": model,
        "base_url": "http://127.0.0.1:11434",
        "credential_ref": None,
        "private_data_allowed": False,
        "timeout_seconds": 30.0,
        "revision": revision,
    }


def _cloud(*, revision: int, model: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "route_kind": "openai_compatible",
        "provider_id": "configured-api",
        "model": model,
        "base_url": "https://api.example.test/v1",
        "credential_ref": "env:NIKA_DEV80_TEST",
        "private_data_allowed": False,
        "timeout_seconds": 30.0,
        "revision": revision,
    }


def _selection(payload: dict[str, object]) -> ModelSelection:
    return ModelSelection.model_validate(
        {key: value for key, value in payload.items() if key != "revision"}
    )


def _selection_id(selection: ModelSelection) -> str:
    return hashlib.sha256(selection.canonical_json().encode("utf-8")).hexdigest()


def _configure(settings: V01ModelSettings, payload: dict[str, object]) -> None:
    result = settings.configure(payload)
    assert result.status == "completed", result.message


def _strategy(candidate_id: str, artifact_ref: str) -> StrategyRef:
    return StrategyRef(
        candidate_id=candidate_id,
        version="1",
        artifact_kind=ArtifactKind.CONFIG,
        artifact_ref=artifact_ref,
        permission_fingerprint="perm-v1",
    )


def _assert_selection_preserved(
    store: SQLiteStore,
    selection_id: str,
    selection: ModelSelection,
) -> None:
    with store.connection() as conn:
        row = conn.execute(
            "SELECT selection_json FROM v01_model_selections WHERE selection_id = ?",
            (selection_id,),
        ).fetchone()
    assert row is not None
    assert row["selection_json"] == selection.canonical_json()


def test_m8_selection_preserves_previous_config_ref_and_does_not_silently_activate(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    settings = V01ModelSettings(store)
    stable_payload = _local(revision=0, model="stable-v1")
    stable = _selection(stable_payload)
    stable_id = _selection_id(stable)
    _configure(settings, stable_payload)

    candidate = _selection(_local(revision=1, model="candidate-v2"))
    candidate_id = _selection_id(candidate)
    repository = SQLiteExperimentRepository(store)
    engine = ExperimentEngine(repository)
    definition = ExperimentDefinition(
        experiment_id="dev80-rollback",
        champion=_strategy("stable", f"model-selection://{stable_id}"),
        challengers=(
            _strategy("candidate", f"model-selection://{candidate_id}"),
        ),
        replays=(ReplayCase("r1", "dataset://dev80", "v1"),),
        policy=PromotionPolicy(
            primary_metric="quality",
            minimum_improvement=0.05,
        ),
    )
    engine.create(definition)
    engine.start(definition.experiment_id)
    engine.record(
        definition.experiment_id,
        MetricObservation("stable", "r1", "quality", 0.70),
    )
    engine.record(
        definition.experiment_id,
        MetricObservation("candidate", "r1", "quality", 0.80),
    )

    selected = engine.complete(definition.experiment_id)
    assert selected.status is ExperimentStatus.PROMOTED
    assert selected.selected_candidate_id == "candidate"
    assert selected.previous_champion_id == "stable"

    recovered = SQLiteExperimentRepository(store).get(definition.experiment_id)
    assert recovered.selected_candidate_id == "candidate"
    assert recovered.previous_champion_id == "stable"
    assert recovered.definition.champion.artifact_ref == f"model-selection://{stable_id}"

    production = V01ModelSettings(store).snapshot()
    assert production["status"] == "ready"
    assert production["model"] == "stable-v1"


def test_successful_default_switch_retains_previous_identity_across_restart(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    settings = V01ModelSettings(store)
    stable_payload = _local(revision=0, model="stable-v1")
    stable = _selection(stable_payload)
    stable_id = _selection_id(stable)
    _configure(settings, stable_payload)

    candidate_payload = _local(revision=1, model="candidate-v2")
    _configure(settings, candidate_payload)

    restarted = V01ModelSettings(store)
    current = restarted.snapshot()
    assert current["status"] == "ready"
    assert current["revision"] == 2
    assert current["model"] == "candidate-v2"
    _assert_selection_preserved(store, stable_id, stable)


def test_failed_runtime_activation_does_not_destroy_previous_selection_identity(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    settings = V01ModelSettings(store)
    stable_payload = _local(revision=0, model="stable-v1")
    stable = _selection(stable_payload)
    stable_id = _selection_id(stable)
    _configure(settings, stable_payload)

    _configure(settings, _cloud(revision=1, model="candidate-v2"))
    payload = settings.prepare_task_payload({"command": "Use candidate."})
    task_id = TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload=payload,
    ).task_id

    factory = V01BoundModelRuntimeFactory(
        store=store,
        definitions=AgentDefinitionRepository(store),
        settings=settings,
    )
    with pytest.raises(ModelSetupError, match="заборонено політикою Nika"):
        factory.for_task(task_id)

    restarted = V01ModelSettings(store)
    current = restarted.snapshot()
    assert current["status"] == "ready"
    assert current["model"] == "candidate-v2"
    _assert_selection_preserved(store, stable_id, stable)


def test_failed_candidate_configuration_commit_keeps_previous_default(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    settings = V01ModelSettings(store)
    stable_payload = _local(revision=0, model="stable-v1")
    _configure(settings, stable_payload)

    with store.connection() as conn:
        conn.execute(
            "CREATE TRIGGER dev80_block_model_switch "
            "BEFORE UPDATE ON v01_model_settings "
            "BEGIN SELECT RAISE(ABORT, 'candidate activation failed'); END"
        )

    result = settings.configure(_local(revision=1, model="candidate-v2"))
    assert result.status == "rejected"

    recovered = V01ModelSettings(store).snapshot()
    assert recovered["status"] == "ready"
    assert recovered["revision"] == 1
    assert recovered["model"] == "stable-v1"
