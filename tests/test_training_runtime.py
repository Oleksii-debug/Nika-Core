from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.checkpoint import CheckpointService
from nika_core.learning_package import FrozenLearningPackage, LearningDataSplit, LearningShard
from nika_core.research.blobs import ContentAddressedBlobStore
from nika_core.resources.contracts import ResourceBudget, ResourceSnapshot
from nika_core.resources.manager import ResourceManager
from nika_core.training_materials import (
    ResolvedTrainingPackage,
    TrainingMaterialEvidence,
    TrainingMaterialSetEvidence,
    resolve_training_materials,
)
from nika_core.training_scale import (
    TrainingScaleError,
    TrainingScalePlan,
    TrainingScaleTier,
    authorize_training_scale,
)
from nika_core.training_runtime import (
    ArtifactIdentity,
    TrainingCheckpointError,
    TrainingControl,
    TrainingJobSpec,
    TrainingRunState,
    TrainingRuntime,
    TrainingStepResult,
    TrainingWorkerError,
    TrainingWorkerFailureEffect,
    training_job_fingerprint,
)

_WORKSPACE_ID = "runtime-training"
_TRAINING_BODY = b'{"prompt":"train","response":"ok"}\n'
_VALIDATION_BODY = b'{"prompt":"validate","response":"ok"}\n'


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


_EXECUTION_PLAN_SHA256 = _sha256(b"runtime-test-execution-plan")


def _frozen_package() -> FrozenLearningPackage:
    training = LearningShard(
        split=LearningDataSplit.TRAINING,
        artifact_sha256=_sha256(_TRAINING_BODY),
        provenance_sha256=_sha256(b"training-provenance"),
        license_evidence_sha256=_sha256(b"training-license"),
        record_count=1,
        byte_count=len(_TRAINING_BODY),
    )
    validation = LearningShard(
        split=LearningDataSplit.VALIDATION,
        artifact_sha256=_sha256(_VALIDATION_BODY),
        provenance_sha256=_sha256(b"validation-provenance"),
        license_evidence_sha256=_sha256(b"validation-license"),
        record_count=1,
        byte_count=len(_VALIDATION_BODY),
    )
    return FrozenLearningPackage.freeze(
        package_id="runtime-package",
        package_version="1",
        base_artifact_sha256=_sha256(b"base-model"),
        selection_policy_sha256=_sha256(b"selection-policy"),
        verification_sha256=_sha256(b"verification"),
        evaluation_set_sha256=_sha256(b"held-out"),
        shards=(training, validation),
    )


def _material_evidence(
    *,
    workspace_id: str = _WORKSPACE_ID,
) -> TrainingMaterialSetEvidence:
    package = _frozen_package()
    materials = tuple(
        TrainingMaterialEvidence.from_shard(shard) for shard in package.shards
    )
    return TrainingMaterialSetEvidence.from_package(
        package,
        workspace_sha256=_sha256(workspace_id.encode()),
        materials=materials,
    )


def _scale_plan(
    evidence: TrainingMaterialSetEvidence,
    *,
    max_steps: int,
) -> TrainingScalePlan:
    training_records = sum(
        item.record_count
        for item in evidence.materials
        if item.split is LearningDataSplit.TRAINING
    )
    training_bytes = sum(
        item.byte_count
        for item in evidence.materials
        if item.split is LearningDataSplit.TRAINING
    )
    validation_records = sum(
        item.record_count
        for item in evidence.materials
        if item.split is LearningDataSplit.VALIDATION
    )
    validation_bytes = sum(
        item.byte_count
        for item in evidence.materials
        if item.split is LearningDataSplit.VALIDATION
    )
    return TrainingScalePlan(
        plan_id="runtime-test-pilot",
        evaluation_set_sha256=evidence.evaluation_set_sha256,
        tiers=(
            TrainingScaleTier(
                tier_id="pilot",
                max_training_records=training_records,
                max_training_bytes=training_bytes,
                max_validation_records=validation_records,
                max_validation_bytes=validation_bytes,
                max_steps=max_steps,
            ),
        ),
    )


def _scale_authorization(
    *,
    evidence: TrainingMaterialSetEvidence,
    job_id: str,
    base_artifact: ArtifactIdentity,
    candidate_artifact_ref: str,
    execution_plan_sha256: str,
    max_steps: int,
    resource_scope: str = "model_training",
):
    return authorize_training_scale(
        plan=_scale_plan(evidence, max_steps=max_steps),
        tier_id="pilot",
        job_id=job_id,
        base_artifact=base_artifact,
        candidate_artifact_ref=candidate_artifact_ref,
        material_evidence=evidence,
        execution_plan_sha256=execution_plan_sha256,
        max_steps=max_steps,
        resource_scope=resource_scope,
    )


def _resolved_materials(
    root: Path,
    *,
    workspace_id: str = _WORKSPACE_ID,
) -> ResolvedTrainingPackage:
    blob_store = ContentAddressedBlobStore(root / f"materials-{_sha256(workspace_id.encode())}")
    blob_store.put_bytes(workspace_id, _TRAINING_BODY)
    blob_store.put_bytes(workspace_id, _VALIDATION_BODY)
    return resolve_training_materials(
        _frozen_package(),
        workspace_id=workspace_id,
        blob_store=blob_store,
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
    execution_plan_sha256: str = field(
        default=_EXECUTION_PLAN_SHA256,
        kw_only=True,
    )
    fail_once_at: int | None = None
    calls: list[int] = field(default_factory=list)
    seen_states: list[dict[str, object]] = field(default_factory=list)
    seen_materials: list[ResolvedTrainingPackage] = field(default_factory=list)
    _failed: bool = False

    def step(
        self,
        *,
        spec: TrainingJobSpec,
        step_index: int,
        resume_state: dict[str, object],
        training_materials: ResolvedTrainingPackage,
    ) -> TrainingStepResult:
        del spec
        self.calls.append(step_index)
        self.seen_states.append(dict(resume_state))
        self.seen_materials.append(training_materials)
        if self.fail_once_at == step_index and not self._failed:
            self._failed = True
            raise RuntimeError("simulated trainer failure")
        completed = step_index == self.complete_at
        return TrainingStepResult(
            resume_state={"last_step": step_index},
            completed=completed,
            candidate_sha256="b" * 64 if completed else None,
        )


@dataclass
class _TypedFailureWorker:
    effect: TrainingWorkerFailureEffect
    execution_plan_sha256: str = field(
        default=_EXECUTION_PLAN_SHA256,
        kw_only=True,
    )
    calls: list[int] = field(default_factory=list)

    def step(
        self,
        *,
        spec: TrainingJobSpec,
        step_index: int,
        resume_state: dict[str, object],
        training_materials: ResolvedTrainingPackage,
    ) -> TrainingStepResult:
        del spec, resume_state, training_materials
        self.calls.append(step_index)
        raise TrainingWorkerError("simulated_failure", effect=self.effect)


@dataclass
class _InvalidResultWorker:
    execution_plan_sha256: str = field(
        default=_EXECUTION_PLAN_SHA256,
        kw_only=True,
    )
    calls: list[int] = field(default_factory=list)

    def step(
        self,
        *,
        spec: TrainingJobSpec,
        step_index: int,
        resume_state: dict[str, object],
        training_materials: ResolvedTrainingPackage,
    ) -> TrainingStepResult:
        del spec, resume_state, training_materials
        self.calls.append(step_index)
        return object()  # type: ignore[return-value]


@dataclass
class _ResourcePressureWorker:
    observer: _Observer
    execution_plan_sha256: str = field(
        default=_EXECUTION_PLAN_SHA256,
        kw_only=True,
    )
    calls: list[int] = field(default_factory=list)

    def step(
        self,
        *,
        spec: TrainingJobSpec,
        step_index: int,
        resume_state: dict[str, object],
        training_materials: ResolvedTrainingPackage,
    ) -> TrainingStepResult:
        del spec, resume_state, training_materials
        self.calls.append(step_index)
        self.observer.cpu_percent = 95.0
        return TrainingStepResult(
            resume_state={"last_step": step_index},
            completed=False,
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
    scale_execution_plan_sha256 = overrides.pop(
        "scale_execution_plan_sha256",
        _EXECUTION_PLAN_SHA256,
    )
    values: dict[str, object] = {
        "job_id": "job-1",
        "task_id": "training-task",
        "project_id": "project-1",
        "owner_id": "owner-1",
        "base_artifact": ArtifactIdentity(
            "models/base",
            _frozen_package().base_artifact_sha256,
        ),
        "frozen_package_sha256": _frozen_package().manifest_sha256,
        "training_material_sha256": _material_evidence().training_material_sha256,
        "candidate_artifact_ref": "models/candidate/job-1",
        "max_steps": 4,
    }
    values.update(overrides)
    try:
        authorization = _scale_authorization(
            evidence=_material_evidence(),
            job_id=values["job_id"],  # type: ignore[arg-type]
            base_artifact=values["base_artifact"],  # type: ignore[arg-type]
            candidate_artifact_ref=values["candidate_artifact_ref"],  # type: ignore[arg-type]
            execution_plan_sha256=scale_execution_plan_sha256,  # type: ignore[arg-type]
            max_steps=values["max_steps"],  # type: ignore[arg-type]
            resource_scope=values.get("resource_scope", "model_training"),  # type: ignore[arg-type]
        )
        values["scale_authorization_sha256"] = authorization.authorization_sha256
    except (TrainingScaleError, TypeError, ValueError):
        values["scale_authorization_sha256"] = "0" * 64
    return TrainingJobSpec(**values)  # type: ignore[arg-type]


def _replace_latest_checkpoint_field(
    store: SQLiteStore,
    *,
    field_name: str,
    value: object,
) -> None:
    with store.connection() as conn:
        row = conn.execute(
            """
            SELECT checkpoint_id, payload_json
            FROM checkpoints
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """
        ).fetchone()
        assert row is not None
        payload = json.loads(row[1])
        payload[field_name] = value
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        checksum = hashlib.sha256(body.encode()).hexdigest()
        conn.execute(
            "UPDATE checkpoints SET payload_json = ?, checksum_sha256 = ? "
            "WHERE checkpoint_id = ?",
            (body, checksum, row[0]),
        )


@dataclass
class _AuthorizedRuntime:
    runtime: TrainingRuntime
    materials: ResolvedTrainingPackage

    def run(
        self,
        spec: TrainingJobSpec,
        worker,
        *,
        control=None,
    ):
        observed_plan = getattr(worker, "execution_plan_sha256", None)
        try:
            authorization = _scale_authorization(
                evidence=self.materials.evidence,
                job_id=spec.job_id,
                base_artifact=spec.base_artifact,
                candidate_artifact_ref=spec.candidate_artifact_ref,
                execution_plan_sha256=observed_plan,
                max_steps=spec.max_steps,
                resource_scope=spec.resource_scope,
            )
        except (TrainingScaleError, TypeError, ValueError):
            authorization = _scale_authorization(
                evidence=self.materials.evidence,
                job_id=spec.job_id,
                base_artifact=spec.base_artifact,
                candidate_artifact_ref=spec.candidate_artifact_ref,
                execution_plan_sha256=_EXECUTION_PLAN_SHA256,
                max_steps=spec.max_steps,
                resource_scope=spec.resource_scope,
            )
        return self.runtime.run(
            spec,
            worker,
            scale_authorization=authorization,
            control=control,
        )


def _authorized_runtime(
    runtime: TrainingRuntime,
    materials: ResolvedTrainingPackage,
) -> _AuthorizedRuntime:
    return _AuthorizedRuntime(runtime=runtime, materials=materials)


def _runtime(
    store: SQLiteStore,
    observer: _Observer | None = None,
    *,
    training_materials: ResolvedTrainingPackage | None = None,
) -> _AuthorizedRuntime:
    resources = ResourceManager(store, observer or _Observer())
    materials = training_materials or _resolved_materials(store.path.parent)
    return _authorized_runtime(
        TrainingRuntime(
            resources=resources,
            checkpoints=CheckpointService(store),
            training_materials=materials,
        ),
        materials,
    )


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
    assert paused.execution_plan_sha256 == first_worker.execution_plan_sha256
    assert paused.job_fingerprint == training_job_fingerprint(
        spec,
        execution_plan_sha256=first_worker.execution_plan_sha256,
    )
    assert first_worker.calls == [0]

    restarted_store = SQLiteStore(store.path)
    second_worker = _Worker(complete_at=1)
    completed = _runtime(restarted_store).run(spec, second_worker)

    assert completed.state is TrainingRunState.COMPLETED
    assert completed.next_step == 2
    assert completed.candidate_sha256 == "b" * 64
    assert second_worker.calls == [1]
    assert second_worker.seen_states == [{"last_step": 0}]


def test_restart_rejects_different_worker_execution_plan_before_effect(
    tmp_path: Path,
) -> None:
    store = _store_with_task(tmp_path / "nika.db")
    spec = _spec(scale_execution_plan_sha256="a" * 64)
    first = _Worker(complete_at=3, execution_plan_sha256="a" * 64)
    paused = _runtime(store).run(
        spec,
        first,
        control=_scripted_control(
            TrainingControl.CONTINUE,
            TrainingControl.CONTINUE,
            TrainingControl.PAUSE,
        ),
    )
    assert paused.state is TrainingRunState.PAUSED

    replacement = _Worker(complete_at=1, execution_plan_sha256="b" * 64)
    with pytest.raises(TrainingScaleError, match="exact training job"):
        _runtime(SQLiteStore(store.path)).run(spec, replacement)

    assert replacement.calls == []


def test_malformed_worker_execution_plan_fails_before_checkpoint_or_effect(
    tmp_path: Path,
) -> None:
    store = _store_with_task(tmp_path / "nika.db")
    spec = _spec()
    worker = _Worker(complete_at=0, execution_plan_sha256="A" * 64)

    with pytest.raises(ValueError, match="execution plan"):
        _runtime(store).run(spec, worker)

    assert CheckpointService(store).latest(spec.task_id) is None
    assert worker.calls == []


def test_scale_authority_rejects_base_identity_change_before_restore(
    tmp_path: Path,
) -> None:
    store = _store_with_task(tmp_path / "nika.db")
    original = _spec()
    _runtime(store).run(
        original,
        _Worker(complete_at=2),
        control=_scripted_control(TrainingControl.PAUSE),
    )
    changed = _spec(base_artifact=ArtifactIdentity("models/base", "c" * 64))

    with pytest.raises(TrainingScaleError, match="base artifact does not match package"):
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


def test_typed_no_effect_worker_failure_is_resumable(tmp_path: Path) -> None:
    store = _store_with_task(tmp_path / "nika.db")
    spec = _spec()
    failed_worker = _TypedFailureWorker(TrainingWorkerFailureEffect.NO_EFFECT)

    failed = _runtime(store).run(spec, failed_worker)
    assert failed.state is TrainingRunState.FAILED
    assert failed.next_step == 0
    assert failed.reason == "worker_no_effect:simulated_failure"

    replacement = _Worker(complete_at=0)
    recovered = _runtime(SQLiteStore(store.path)).run(spec, replacement)
    assert recovered.state is TrainingRunState.COMPLETED
    assert replacement.calls == [0]


def test_typed_unknown_worker_failure_requires_reconciliation(tmp_path: Path) -> None:
    store = _store_with_task(tmp_path / "nika.db")
    spec = _spec()
    failed_worker = _TypedFailureWorker(TrainingWorkerFailureEffect.UNKNOWN)

    failed = _runtime(store).run(spec, failed_worker)
    assert failed.state is TrainingRunState.RECONCILE_REQUIRED
    assert failed.next_step == 0
    assert failed.reason == "worker_unknown:simulated_failure"

    replacement = _Worker(complete_at=0)
    restarted = _runtime(SQLiteStore(store.path)).run(spec, replacement)
    assert restarted.state is TrainingRunState.RECONCILE_REQUIRED
    assert replacement.calls == []


def test_untyped_worker_crash_is_never_blindly_replayed(tmp_path: Path) -> None:
    store = _store_with_task(tmp_path / "nika.db")
    spec = _spec()
    crashing = _Worker(complete_at=2, fail_once_at=0)

    with pytest.raises(RuntimeError, match="simulated trainer failure"):
        _runtime(store).run(spec, crashing)

    replacement = _Worker(complete_at=0)
    restarted = _runtime(SQLiteStore(store.path)).run(spec, replacement)
    assert restarted.state is TrainingRunState.RECONCILE_REQUIRED
    assert restarted.reason == "previous_worker_effect_unknown"
    assert replacement.calls == []


def test_invalid_worker_result_requires_reconciliation(tmp_path: Path) -> None:
    store = _store_with_task(tmp_path / "nika.db")
    spec = _spec()
    invalid = _InvalidResultWorker()

    result = _runtime(store).run(spec, invalid)

    assert result.state is TrainingRunState.RECONCILE_REQUIRED
    assert result.reason == "worker_unknown:invalid_result"
    assert invalid.calls == [0]


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
    materials = _resolved_materials(store.path.parent)
    runtime = _authorized_runtime(
        TrainingRuntime(
            resources=resources,
            checkpoints=CheckpointService(store),
            training_materials=materials,
        ),
        materials,
    )
    worker = _Worker(complete_at=0)

    waiting = runtime.run(_spec(), worker)

    assert waiting.state is TrainingRunState.WAITING
    assert waiting.reason == "cpu_limit"
    assert waiting.queue_position == 1
    assert worker.calls == []
    assert resources.cancel_waiting(
        scope="model_training", owner_id="owner-1", request_id="job-1"
    )


def test_live_resource_pressure_pauses_before_next_training_effect_and_resumes(
    tmp_path: Path,
) -> None:
    store = _store_with_task(tmp_path / "nika.db")
    observer = _Observer()
    resources = ResourceManager(store, observer)
    resources.set_budget(
        ResourceBudget(
            scope="model_training",
            owner_id="owner-1",
            max_concurrent=1,
            max_cpu_percent=50.0,
        )
    )
    materials = _resolved_materials(store.path.parent)
    runtime = _authorized_runtime(
        TrainingRuntime(
            resources=resources,
            checkpoints=CheckpointService(store),
            training_materials=materials,
        ),
        materials,
    )
    spec = _spec()
    first_worker = _ResourcePressureWorker(observer)

    paused = runtime.run(spec, first_worker)

    assert paused.state is TrainingRunState.PAUSED
    assert paused.next_step == 1
    assert paused.reason == "resource_revalidation:cpu_limit"
    assert first_worker.calls == [0]
    assert resources.active_count(
        scope="model_training", owner_id="owner-1"
    ) == 0

    observer.cpu_percent = 5.0
    second_worker = _Worker(complete_at=1)
    completed = _authorized_runtime(
        TrainingRuntime(
            resources=resources,
            checkpoints=CheckpointService(store),
            training_materials=materials,
        ),
        materials,
    ).run(spec, second_worker)

    assert completed.state is TrainingRunState.COMPLETED
    assert completed.next_step == 2
    assert second_worker.calls == [1]
    assert second_worker.seen_states == [{"last_step": 0}]


def test_resource_revalidation_never_treats_own_concurrency_slot_as_pressure(
    tmp_path: Path,
) -> None:
    store = _store_with_task(tmp_path / "nika.db")
    resources = ResourceManager(store, _Observer())
    resources.set_budget(
        ResourceBudget(
            scope="model_training",
            owner_id="owner-1",
            max_concurrent=1,
            max_cpu_percent=80.0,
            max_memory_percent=80.0,
        )
    )
    worker = _Worker(complete_at=0)
    runtime = TrainingRuntime(
        resources=resources,
        checkpoints=CheckpointService(store),
        training_materials=_resolved_materials(store.path.parent),
    )

    completed = runtime.run(_spec(), worker)

    assert completed.state is TrainingRunState.COMPLETED
    assert worker.calls == [0]


def test_max_steps_is_hard_bound_and_never_promotes(tmp_path: Path) -> None:
    store = _store_with_task(tmp_path / "nika.db")
    spec = _spec(max_steps=2)
    worker = _Worker(complete_at=99)

    exhausted = _runtime(store).run(spec, worker)

    assert exhausted.state is TrainingRunState.EXHAUSTED
    assert exhausted.next_step == 2
    assert exhausted.reason == "max_steps_exhausted"
    assert worker.calls == [0, 1]


def test_frozen_package_identity_is_bound_across_restart(tmp_path: Path) -> None:
    store = _store_with_task(tmp_path / "nika.db")
    original = _spec()
    _runtime(store).run(
        original,
        _Worker(complete_at=2),
        control=_scripted_control(TrainingControl.PAUSE),
    )

    changed = _spec(frozen_package_sha256="e" * 64)
    with pytest.raises(TrainingScaleError, match="exact training job"):
        _runtime(SQLiteStore(store.path)).run(changed, _Worker(complete_at=0))


def test_waiting_evidence_binds_frozen_package_identity(tmp_path: Path) -> None:
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
    runtime = TrainingRuntime(
        resources=resources,
        checkpoints=CheckpointService(store),
        training_materials=_resolved_materials(store.path.parent),
    )
    spec = _spec()

    waiting = runtime.run(spec, _Worker(complete_at=0))

    assert waiting.state is TrainingRunState.WAITING
    assert waiting.frozen_package_sha256 == spec.frozen_package_sha256
    assert waiting.training_material_sha256 == spec.training_material_sha256
    assert waiting.scale_authorization_sha256 == spec.scale_authorization_sha256


def test_completed_checkpoint_requires_candidate_digest_on_restore(tmp_path: Path) -> None:
    store = _store_with_task(tmp_path / "nika.db")
    spec = _spec()
    completed = _runtime(store).run(spec, _Worker(complete_at=0))
    assert completed.state is TrainingRunState.COMPLETED

    _replace_latest_checkpoint_field(store, field_name="candidate_sha256", value=None)

    with pytest.raises(TrainingCheckpointError, match="result evidence"):
        _runtime(SQLiteStore(store.path)).run(spec, _Worker(complete_at=0))


def test_checkpoint_rejects_boolean_step_carrier(tmp_path: Path) -> None:
    store = _store_with_task(tmp_path / "nika.db")
    spec = _spec()
    _runtime(store).run(
        spec,
        _Worker(complete_at=2),
        control=_scripted_control(TrainingControl.PAUSE),
    )
    _replace_latest_checkpoint_field(store, field_name="next_step", value=True)

    with pytest.raises(TrainingCheckpointError, match="invalid training checkpoint step"):
        _runtime(SQLiteStore(store.path)).run(spec, _Worker(complete_at=0))


def test_control_callback_requires_exact_training_control(tmp_path: Path) -> None:
    store = _store_with_task(tmp_path / "nika.db")

    with pytest.raises(TypeError, match="exact TrainingControl"):
        _runtime(store).run(
            _spec(),
            _Worker(complete_at=0),
            control=lambda: "continue",  # type: ignore[return-value]
        )



def test_worker_receives_exact_resolved_material_package(tmp_path: Path) -> None:
    store = _store_with_task(tmp_path / "nika.db")
    materials = _resolved_materials(tmp_path)
    worker = _Worker(complete_at=0)

    completed = _runtime(store, training_materials=materials).run(_spec(), worker)

    assert completed.state is TrainingRunState.COMPLETED
    assert worker.seen_materials == [materials]
    assert completed.training_material_sha256 == materials.training_material_sha256


def test_material_digest_is_bound_across_restart(tmp_path: Path) -> None:
    store = _store_with_task(tmp_path / "nika.db")
    original = _spec()
    _runtime(store).run(
        original,
        _Worker(complete_at=2),
        control=_scripted_control(TrainingControl.PAUSE),
    )

    changed = _spec(training_material_sha256="e" * 64)
    with pytest.raises(TrainingScaleError, match="exact training job"):
        _runtime(SQLiteStore(store.path)).run(changed, _Worker(complete_at=0))


def test_checkpoint_rejects_scale_authorization_tamper(tmp_path: Path) -> None:
    store = _store_with_task(tmp_path / "nika.db")
    spec = _spec()
    _runtime(store).run(
        spec,
        _Worker(complete_at=2),
        control=_scripted_control(TrainingControl.PAUSE),
    )
    _replace_latest_checkpoint_field(
        store,
        field_name="scale_authorization_sha256",
        value="e" * 64,
    )

    with pytest.raises(
        TrainingCheckpointError,
        match="training scale authorization mismatch",
    ):
        _runtime(SQLiteStore(store.path)).run(spec, _Worker(complete_at=0))


def test_legacy_checkpoint_without_scale_authority_fails_closed(tmp_path: Path) -> None:
    store = _store_with_task(tmp_path / "nika.db")
    spec = _spec()
    CheckpointService(store).save(
        task_id=spec.task_id,
        stage="training_runtime/v3/paused",
        payload={"legacy": True},
    )

    with pytest.raises(
        TrainingCheckpointError,
        match="predates scale authorization",
    ):
        _runtime(SQLiteStore(store.path)).run(spec, _Worker(complete_at=0))


def test_checkpoint_rejects_material_digest_tamper(tmp_path: Path) -> None:
    store = _store_with_task(tmp_path / "nika.db")
    spec = _spec()
    _runtime(store).run(
        spec,
        _Worker(complete_at=2),
        control=_scripted_control(TrainingControl.PAUSE),
    )
    _replace_latest_checkpoint_field(
        store,
        field_name="training_material_sha256",
        value="e" * 64,
    )

    with pytest.raises(TrainingCheckpointError, match="training material identity mismatch"):
        _runtime(SQLiteStore(store.path)).run(spec, _Worker(complete_at=0))


def test_physical_material_tamper_fails_before_worker_effect(tmp_path: Path) -> None:
    store = _store_with_task(tmp_path / "nika.db")
    materials = _resolved_materials(tmp_path)
    target = materials.materials[0].path
    target.write_bytes(b"x" * target.stat().st_size)
    worker = _Worker(complete_at=0)

    failed = _runtime(store, training_materials=materials).run(_spec(), worker)

    assert failed.state is TrainingRunState.FAILED
    assert failed.reason == "training_material_verification_failed"
    assert failed.next_step == 0
    assert worker.calls == []


def test_wrong_workspace_material_identity_fails_before_worker_effect(
    tmp_path: Path,
) -> None:
    store = _store_with_task(tmp_path / "nika.db")
    materials = _resolved_materials(tmp_path, workspace_id="different-workspace")
    worker = _Worker(complete_at=0)

    failed = _runtime(store, training_materials=materials).run(_spec(), worker)

    assert failed.state is TrainingRunState.FAILED
    assert failed.reason == "training_material_identity_mismatch"
    assert worker.calls == []


@dataclass
class _DriftingExecutionPlanWorker:
    execution_plan_sha256: str = "a" * 64
    calls: list[int] = field(default_factory=list)

    def step(
        self,
        *,
        spec: TrainingJobSpec,
        step_index: int,
        resume_state: dict[str, object],
        training_materials: ResolvedTrainingPackage,
    ) -> TrainingStepResult:
        del spec, resume_state, training_materials
        self.calls.append(step_index)
        self.execution_plan_sha256 = "b" * 64
        return TrainingStepResult(
            resume_state={"last_step": step_index},
            completed=False,
        )


def test_execution_plan_drift_fails_before_next_worker_effect(tmp_path: Path) -> None:
    store = _store_with_task(tmp_path / "nika.db")
    worker = _DriftingExecutionPlanWorker()

    failed = _runtime(store).run(
        _spec(scale_execution_plan_sha256="a" * 64),
        worker,
    )

    assert failed.state is TrainingRunState.FAILED
    assert failed.reason == "training_execution_plan_changed"
    assert failed.next_step == 1
    assert failed.execution_plan_sha256 == "a" * 64
    assert worker.calls == [0]


@dataclass
class _TamperingWorker:
    execution_plan_sha256: str = field(
        default=_EXECUTION_PLAN_SHA256,
        kw_only=True,
    )
    calls: list[int] = field(default_factory=list)

    def step(
        self,
        *,
        spec: TrainingJobSpec,
        step_index: int,
        resume_state: dict[str, object],
        training_materials: ResolvedTrainingPackage,
    ) -> TrainingStepResult:
        del spec, resume_state
        self.calls.append(step_index)
        if step_index == 0:
            target = training_materials.materials[0].path
            target.write_bytes(b"z" * target.stat().st_size)
        return TrainingStepResult(
            resume_state={"last_step": step_index},
            completed=False,
        )


def test_reverify_runs_before_every_training_effect(tmp_path: Path) -> None:
    store = _store_with_task(tmp_path / "nika.db")
    materials = _resolved_materials(tmp_path)
    worker = _TamperingWorker()

    failed = _runtime(store, training_materials=materials).run(_spec(), worker)

    assert failed.state is TrainingRunState.FAILED
    assert failed.reason == "training_material_verification_failed"
    assert failed.next_step == 1
    assert worker.calls == [0]


def test_material_manifest_mismatch_fails_before_worker_effect(tmp_path: Path) -> None:
    store = _store_with_task(tmp_path / "nika.db")
    materials = _resolved_materials(tmp_path)
    worker = _Worker(complete_at=0)
    spec = _spec(
        frozen_package_sha256="e" * 64,
        training_material_sha256=materials.training_material_sha256,
    )

    failed = _runtime(store, training_materials=materials).run(spec, worker)

    assert failed.state is TrainingRunState.FAILED
    assert failed.reason == "training_material_identity_mismatch"
    assert worker.calls == []


def test_runtime_rejects_noncanonical_material_package(tmp_path: Path) -> None:
    store = _store_with_task(tmp_path / "nika.db")
    resources = ResourceManager(store, _Observer())

    with pytest.raises(TypeError, match="exact ResolvedTrainingPackage"):
        TrainingRuntime(
            resources=resources,
            checkpoints=CheckpointService(store),
            training_materials=object(),  # type: ignore[arg-type]
        )
