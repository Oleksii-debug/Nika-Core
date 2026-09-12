from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.checkpoint import CheckpointService
from nika_core.resources.contracts import ResourceBudget, ResourceSnapshot
from nika_core.resources.manager import ResourceManager
from nika_core.training_runtime import (
    ArtifactIdentity,
    TrainingCheckpointError,
    TrainingControl,
    TrainingJobSpec,
    TrainingRunState,
    TrainingRuntime,
    TrainingStepResult,
)


@dataclass
class _Observer:
    cpu_percent: float = 5.0
    memory_percent: float = 10.0

    def snapshot(self) -> ResourceSnapshot:
        return ResourceSnapshot(
            cpu_percent=self.cpu_percent,
            memory_percent=self.memory_percent,
            available_memory_bytes=8_000_000_000,
        )


@dataclass
class _Worker:
    complete_at: int
    fail_once_at: int | None = None
    calls: list[int] = field(default_factory=list)
    seen_states: list[dict[str, object]] = field(default_factory=list)
    _failed: bool = False

    def step(
        self,
        *,
        spec: TrainingJobSpec,
        step_index: int,
        resume_state: dict[str, object],
    ) -> TrainingStepResult:
        del spec
        self.calls.append(step_index)
        self.seen_states.append(dict(resume_state))
        if self.fail_once_at == step_index and not self._failed:
            self._failed = True
            raise RuntimeError("simulated trainer failure")
        completed = step_index == self.complete_at
        return TrainingStepResult(
            resume_state={"last_step": step_index},
            completed=completed,
            candidate_sha256="b" * 64 if completed else None,
        )


def _store_with_task(path: Path, task_id: str = "training-task") -> SQLiteStore:
    store = SQLiteStore(path)
    store.initialize()
    now = datetime.now(UTC).isoformat()
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO tasks(
                task_id, workspace_id, agent_id, state, payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (task_id, "training", "training-runtime", "pending", "{}", now, now),
        )
    return store


def _spec(**overrides: object) -> TrainingJobSpec:
    values: dict[str, object] = {
        "job_id": "job-1",
        "task_id": "training-task",
        "project_id": "project-1",
        "owner_id": "owner-1",
        "base_artifact": ArtifactIdentity("models/base", "a" * 64),
        "candidate_artifact_ref": "models/candidate/job-1",
        "max_steps": 4,
    }
    values.update(overrides)
    return TrainingJobSpec(**values)  # type: ignore[arg-type]


def _runtime(store: SQLiteStore, observer: _Observer | None = None) -> TrainingRuntime:
    resources = ResourceManager(store, observer or _Observer())
    return TrainingRuntime(resources=resources, checkpoints=CheckpointService(store))


def _scripted_control(*actions: TrainingControl):
    remaining = iter(actions)

    def control() -> TrainingControl:
        return next(remaining, TrainingControl.CONTINUE)

    return control


def test_candidate_cannot_overwrite_base_artifact() -> None:
    base = ArtifactIdentity("models/base", "a" * 64)
    with pytest.raises(ValueError, match="must not overwrite"):
        _spec(base_artifact=base, candidate_artifact_ref=base.artifact_ref)


def test_pause_restart_resume_completes_from_durable_checkpoint(tmp_path: Path) -> None:
    store = _store_with_task(tmp_path / "nika.db")
    spec = _spec()
    first_worker = _Worker(complete_at=3)

    paused = _runtime(store).run(
        spec,
        first_worker,
        control=_scripted_control(
            TrainingControl.CONTINUE,
            TrainingControl.CONTINUE,
            TrainingControl.PAUSE,
        ),
    )

    assert paused.state is TrainingRunState.PAUSED
    assert paused.next_step == 1
    assert first_worker.calls == [0]

    restarted_store = SQLiteStore(store.path)
    second_worker = _Worker(complete_at=1)
    completed = _runtime(restarted_store).run(spec, second_worker)

    assert completed.state is TrainingRunState.COMPLETED
    assert completed.next_step == 2
    assert completed.candidate_sha256 == "b" * 64
    assert second_worker.calls == [1]
    assert second_worker.seen_states == [{"last_step": 0}]


def test_checkpoint_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    store = _store_with_task(tmp_path / "nika.db")
    original = _spec()
    _runtime(store).run(
        original,
        _Worker(complete_at=2),
        control=_scripted_control(TrainingControl.PAUSE),
    )
    changed = _spec(base_artifact=ArtifactIdentity("models/base", "c" * 64))

    with pytest.raises(TrainingCheckpointError, match="identity mismatch"):
        _runtime(store).run(changed, _Worker(complete_at=0))


def test_cancel_is_durable_and_does_not_restart_worker(tmp_path: Path) -> None:
    store = _store_with_task(tmp_path / "nika.db")
    spec = _spec()
    worker = _Worker(complete_at=0)

    cancelled = _runtime(store).run(
        spec,
        worker,
        control=_scripted_control(TrainingControl.CANCEL),
    )
    assert cancelled.state is TrainingRunState.CANCELLED
    assert worker.calls == []

    after_restart = _runtime(SQLiteStore(store.path)).run(spec, worker)
    assert after_restart.state is TrainingRunState.CANCELLED
    assert worker.calls == []


def test_worker_failure_is_checkpointed_and_resumable(tmp_path: Path) -> None:
    store = _store_with_task(tmp_path / "nika.db")
    spec = _spec()
    failed_worker = _Worker(complete_at=2, fail_once_at=0)

    failed = _runtime(store).run(spec, failed_worker)
    assert failed.state is TrainingRunState.FAILED
    assert failed.next_step == 0
    assert failed.reason == "worker_error:RuntimeError"

    replacement = _Worker(complete_at=0)
    recovered = _runtime(SQLiteStore(store.path)).run(spec, replacement)
    assert recovered.state is TrainingRunState.COMPLETED
    assert replacement.calls == [0]


def test_resource_pressure_waits_without_running_trainer(tmp_path: Path) -> None:
    store = _store_with_task(tmp_path / "nika.db")
    resources = ResourceManager(store, _Observer(cpu_percent=75.0))
    resources.set_budget(
        ResourceBudget(
            scope="model_training",
            owner_id="owner-1",
            max_concurrent=1,
            max_cpu_percent=25.0,
        )
    )
    runtime = TrainingRuntime(resources=resources, checkpoints=CheckpointService(store))
    worker = _Worker(complete_at=0)

    waiting = runtime.run(_spec(), worker)

    assert waiting.state is TrainingRunState.WAITING
    assert waiting.reason == "cpu_limit"
    assert waiting.queue_position == 1
    assert worker.calls == []
    assert resources.cancel_waiting(
        scope="model_training", owner_id="owner-1", request_id="job-1"
    )


def test_max_steps_is_hard_bound_and_never_promotes(tmp_path: Path) -> None:
    store = _store_with_task(tmp_path / "nika.db")
    spec = _spec(max_steps=2)
    worker = _Worker(complete_at=99)

    exhausted = _runtime(store).run(spec, worker)

    assert exhausted.state is TrainingRunState.EXHAUSTED
    assert exhausted.next_step == 2
    assert exhausted.reason == "max_steps_exhausted"
    assert worker.calls == [0, 1]
